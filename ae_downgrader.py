"""
Rewind AE — core engine.

Reads the version stamp of After Effects files (.aep .aet .ffx .aepx) and
rewrites it so the file opens in an older release.  Originals are never
modified: output always goes to a new file / a new sibling folder.

Public API (used by app.py and the CLI at the bottom of this file):

    analyze_bytes(data, ext)           -> dict   version info for one file
    patch_bytes(data, ext, year)       -> dict   patched bytes (or an error)
    scan_source(path)                  -> dict   one dropped file / folder
    scan_paths(paths)                  -> list   many sources at once
    convert(paths, year, ...)          -> dict   do the work (progress + cancel)
    reveal(path)                       -> None   show in Explorer / Finder
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
#  Version tables
# --------------------------------------------------------------------------- #

AE_EXTS = ("aep", "aet", "ffx", "aepx")

# code / update are the two numbers stored in the file's `head` chunk.
# `experimental` = header values for these releases could not be verified
# against real files, so the UI flags them.
TARGETS: Dict[int, Dict[str, Any]] = {
    2026: {"major": 26, "code": 0x61, "update": 0x01, "blurb": "v26 · current"},
    2025: {"major": 25, "code": 0x60, "update": 0x01, "blurb": "v25"},
    2024: {"major": 24, "code": 0x5F, "update": 0x01, "blurb": "v24 · most popular"},
    2023: {"major": 23, "code": 0x5E, "update": 0x01, "blurb": "v23"},
    2022: {"major": 22, "code": 0x5D, "update": 0x2B, "blurb": "v22 · trims layer records"},
    2021: {"major": 18, "code": 0x5D, "update": 0x19, "blurb": "v18 · trims layer records"},
    2020: {"major": 17, "code": 0x5D, "update": 0x18, "blurb": "v17 · trims layer records"},
}

# Marketing version -> AE major, used when an .aepx only carries a version attribute.
MAJOR_TO_YEAR = {13: 2015, 14: 2017, 15: 2018, 16: 2019, 17: 2020, 18: 2021}

JUNK_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}
MAX_FILES_PER_SOURCE = 50000
COPY_CHUNK = 4 * 1024 * 1024
HEAD_READ = 4096
XML_HEAD_READ = 256 * 1024


def target_list() -> List[Dict[str, Any]]:
    """Targets for the UI, newest first."""
    return [
        {
            "year": y,
            "major": t["major"],
            "blurb": t["blurb"],
            "experimental": bool(t.get("experimental")),
        }
        for y, t in sorted(TARGETS.items(), reverse=True)
    ]


def year_from_code(code: int, update: int) -> Tuple[Optional[int], Optional[int]]:
    """Map the (code, update) pair from a `head` chunk to (year, AE major)."""
    if code == 0x61:
        return 2026, 26
    if code == 0x60:
        return 2025, 25
    if code == 0x5F:
        return 2024, 24
    if code == 0x5E:
        return 2023, 23
    if code == 0x5D:
        if update <= 0x18:
            return 2020, 17
        if update <= 0x1D:
            return 2021, 18
        return 2022, 22
    if code == 0x5C:
        return 2019, 16
    if code == 0x5B:
        return 2018, 15
    if code == 0x5A:
        return 2017, 14
    if code == 0x59:
        return 2015, 13
    if code == 0x58:
        return 2014, 13
    if code == 0x56:
        return 2012, 11
    return None, None


# --------------------------------------------------------------------------- #
#  RIFX helpers
# --------------------------------------------------------------------------- #

def find_chunk(b: bytes, target_id: bytes, start: int = 12, limit: int = HEAD_READ) -> Optional[Tuple[int, int]]:
    """Locate a top-level chunk. Returns (offset, size) or None."""
    pos = start
    end = min(len(b), limit)
    while pos + 8 <= end:
        cid = bytes(b[pos:pos + 4])
        size = struct.unpack_from(">I", b, pos + 4)[0]
        if cid == target_id:
            return pos, size
        nxt = pos + 8 + size + (size & 1)
        if nxt <= pos:
            break
        pos = nxt
    return None


def _bad(kind: str, note: str, **extra: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {"kind": kind, "patchable": False, "year": None, "major": None, "note": note}
    d.update(extra)
    return d


def _good(kind: str, year: int, major: int, **extra: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "kind": kind, "patchable": True, "year": year, "major": major,
        "note": f"AE {year}",
    }
    d.update(extra)
    return d


# --------------------------------------------------------------------------- #
#  Analysis
# --------------------------------------------------------------------------- #

_RE_XML_BDATA = re.compile(rb'(<head\b[^>]*?\bbdata\s*=\s*")([0-9A-Fa-f]{8,})(")', re.S)
_RE_XML_ATTR = re.compile(
    rb'(<AfterEffectsProject\b[^>]*?\b(?:b?version)\s*=\s*")([0-9]+(?:\.[0-9]+)*)(")', re.I | re.S
)


def _kind_for_ext(ext: str) -> Optional[str]:
    e = ext.lower().lstrip(".")
    if e in ("aep", "aet"):
        return "project"
    if e == "ffx":
        return "preset"
    if e == "aepx":
        return "xml"
    return None


def analyze_bytes(b: bytes, ext: str) -> Dict[str, Any]:
    """Detect kind + AE version from the first bytes of a file."""
    kind = _kind_for_ext(ext)
    if kind is None:
        return {"kind": "other", "patchable": False, "year": None, "major": None, "note": "Not an After Effects file"}
    if kind == "xml":
        return _analyze_xml(b)
    return _analyze_riff(b, kind)


def _analyze_riff(b: bytes, kind: str) -> Dict[str, Any]:
    if len(b) < 28:
        return _bad(kind, "File is empty or too small")

    magic = 0
    if b[0:4] != b"RIFX":
        magic = b[:32].find(b"RIFX")
        if magic < 0:
            if b[0:4] == b"RIFF":
                return _bad(kind, "Little-endian RIFF — not an After Effects file")
            return _bad(kind, "Not an After Effects file (bad header)")

    form = b[magic + 8:magic + 12].decode("latin1", errors="ignore")
    expected = "FaFX" if kind == "preset" else "Egg!"
    if form != expected:
        return _bad(kind, f"Unexpected container type '{form}'", form=form)

    head = find_chunk(b, b"head", start=magic + 12, limit=magic + HEAD_READ)
    if head is None:
        return _bad(kind, "No version header found", form=form)

    data = head[0] + 8
    try:
        if kind == "preset":
            code = struct.unpack_from(">I", b, data + 4)[0]
            upd = struct.unpack_from(">I", b, data + 8)[0]
        else:
            code, upd = struct.unpack_from(">HH", b, data)
    except struct.error:
        return _bad(kind, "Version header is truncated", form=form)

    year, major = year_from_code(code, upd)
    if year is None or major is None:
        return _bad(kind, f"Unknown version code 0x{code:02X}", form=form, code=code, update=upd)

    return _good(kind, year, major, form=form, code=code, update=upd, magic=magic)


def _analyze_xml(b: bytes) -> Dict[str, Any]:
    head = b[:XML_HEAD_READ]
    m = _RE_XML_BDATA.search(head)
    if m:
        try:
            code, upd = struct.unpack(">HH", bytes.fromhex(m.group(2)[:8].decode("ascii")))
        except (ValueError, struct.error):
            code = upd = -1
        year, major = year_from_code(code, upd)
        if year and major:
            return _good("xml", year, major, code=code, update=upd, xml_mode="bdata")

    m = _RE_XML_ATTR.search(head)
    if m:
        major = int(m.group(2).split(b".")[0])
        year = MAJOR_TO_YEAR.get(major) or (2000 + major if major >= 22 else None)
        if year:
            return _good("xml", year, major, xml_mode="attr")

    return _bad("xml", "No version information found in this .aepx")


def analyze_file(path: str) -> Dict[str, Any]:
    ext = os.path.splitext(path)[1]
    kind = _kind_for_ext(ext)
    if kind is None:
        return analyze_bytes(b"", ext)
    try:
        with open(path, "rb") as f:
            head = f.read(XML_HEAD_READ if kind == "xml" else HEAD_READ)
    except OSError as e:
        return _bad(kind, f"Can't read file ({e.strerror or e})")
    return analyze_bytes(head, ext)


# --------------------------------------------------------------------------- #
#  Patching
# --------------------------------------------------------------------------- #

class _RebuildState:
    def __init__(self) -> None:
        self.trimmed = 0
        self.conflict = False
        self.bad_record = False


def _rebuild_chunks(src: bytes, start: int, end: int, st: _RebuildState) -> bytearray:
    """Re-emit a chunk tree, trimming the 4 extra bytes AE 2023+ adds to `ldta`."""
    out = bytearray()
    pos = start
    while pos + 8 <= end:
        cid = src[pos:pos + 4]
        size = struct.unpack_from(">I", src, pos + 4)[0]
        d0 = pos + 8
        d1 = d0 + size
        if d1 > end:  # truncated tail — keep as is
            out += src[pos:end]
            return out

        cid_s = cid.decode("latin1", errors="ignore")
        if cid_s == "LIST" and size >= 4:
            if src[d0:d0 + 4] == b"btdk":
                data = bytes(src[d0:d1])
            else:
                inner = _rebuild_chunks(src, d0 + 4, d1, st)
                data = bytes(src[d0:d0 + 4]) + bytes(inner)
        elif cid_s == "ldta" and size == 164:
            if any(src[d1 - 4:d1]):
                st.conflict = True
                data = bytes(src[d0:d1])
            else:
                data = bytes(src[d0:d1 - 4])
                st.trimmed += 1
        elif cid_s == "ldta" and size > 164:
            st.bad_record = True
            data = bytes(src[d0:d1])
        else:
            data = bytes(src[d0:d1])

        out += cid
        out += struct.pack(">I", len(data))
        out += data
        if len(data) & 1:
            out += b"\x00"
        pos = d1 + (size & 1)
    return out


def patch_bytes(b: bytes, ext: str, target_year: int) -> Dict[str, Any]:
    """Return {'ok': True, 'bytes': ...} or {'ok': False, 'error': ...}."""
    if target_year not in TARGETS:
        return {"ok": False, "error": f"Unsupported target: AE {target_year}"}

    a = analyze_bytes(b, ext)
    if not a.get("patchable"):
        return {"ok": False, "error": a.get("note", "File can't be patched")}
    if a["year"] <= target_year:
        return {"ok": False, "skip": True, "error": f"Already AE {a['year']} — nothing to do"}

    t = TARGETS[target_year]
    kind = a["kind"]
    needs_rebuild = kind == "project" and a["year"] >= 2023 and target_year <= 2022

    # ---- .aepx (XML) ------------------------------------------------------ #
    if kind == "xml":
        if a["year"] >= 2023 and target_year <= 2022:
            return {"ok": False, "error": "An .aepx from AE 2023+ can't be rewound below AE 2023 — save it as .aep instead"}
        if a.get("xml_mode") == "bdata":
            m = _RE_XML_BDATA.search(b[:XML_HEAD_READ])
            assert m is not None
            old_hex = m.group(2)
            new_hex = f"{t['code']:04X}{t['update']:04X}".encode("ascii")
            if old_hex.islower():  # keep the file's own hex case
                new_hex = new_hex.lower()
            out = b[:m.start(2)] + new_hex + old_hex[8:] + b[m.end(2):]
        else:
            m = _RE_XML_ATTR.search(b[:XML_HEAD_READ])
            assert m is not None
            out = b[:m.start(2)] + f"{t['major']}.0".encode("ascii") + b[m.end(2):]
        return _verified(bytes(out), ext, target_year, "xml")

    # ---- binary (RIFX) ----------------------------------------------------- #
    magic = a.get("magic", 0)

    if not needs_rebuild:
        out = bytearray(b)
        head = find_chunk(out, b"head", start=magic + 12, limit=magic + HEAD_READ)
        if head is None:
            return {"ok": False, "error": "Could not locate the version header"}
        d = head[0] + 8
        if kind == "preset":
            out[d + 4:d + 8] = struct.pack(">I", t["code"])
            out[d + 8:d + 12] = struct.pack(">I", t["update"])
        else:
            out[d:d + 2] = struct.pack(">H", t["code"])
            out[d + 2:d + 4] = struct.pack(">H", t["update"])
        return _verified(bytes(out), ext, target_year, "stamp")

    top_size = struct.unpack_from(">I", b, magic + 4)[0]
    payload_end = min(magic + 8 + top_size, len(b))
    st = _RebuildState()
    inner = _rebuild_chunks(b, magic + 12, payload_end, st)
    if st.conflict:
        return {"ok": False, "error": "Uses AE 2023+ track-matte features that can't exist below AE 2023"}
    if st.bad_record:
        return {"ok": False, "error": "Unsupported layer record size for this source version"}

    out = bytearray(b[:magic])
    out += b"RIFX"
    out += struct.pack(">I", 4 + len(inner))
    out += b"Egg!"
    out += inner
    out += b[payload_end:]

    head = find_chunk(out, b"head", start=magic + 12, limit=magic + HEAD_READ)
    if head is None:
        return {"ok": False, "error": "Rebuild failed: version header missing afterwards"}
    d = head[0] + 8
    out[d:d + 2] = struct.pack(">H", t["code"])
    out[d + 2:d + 4] = struct.pack(">H", t["update"])
    res = _verified(bytes(out), ext, target_year, "rebuild")
    if res.get("ok"):
        res["trimmed"] = st.trimmed
    return res


def _verified(out: bytes, ext: str, target_year: int, method: str) -> Dict[str, Any]:
    """Re-read what we just wrote; refuse to hand back anything that doesn't say `target_year`."""
    v = analyze_bytes(out[:XML_HEAD_READ] if _kind_for_ext(ext) == "xml" else out[:HEAD_READ], ext)
    if v.get("year") != target_year:
        return {"ok": False, "error": "Verification failed — output didn't carry the target version"}
    return {"ok": True, "bytes": out, "method": method, "target_year": target_year}


# --------------------------------------------------------------------------- #
#  Scanning
# --------------------------------------------------------------------------- #

def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower().lstrip(".")


def _is_junk(name: str) -> bool:
    return name.lower() in JUNK_FILES


def _walk(folder: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every file under `folder` (sorted, junk skipped) and every sub-folder (relative)."""
    entries: List[Dict[str, Any]] = []
    subdirs: List[str] = []
    for root, dirs, files in os.walk(folder, onerror=lambda e: None):
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        for d in dirs:
            subdirs.append(os.path.relpath(os.path.join(root, d), folder).replace("\\", "/"))
        for f in files:
            if _is_junk(f):
                continue
            fp = os.path.join(root, f)
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            entries.append({
                "name": f,
                "path": fp,
                "rel": os.path.relpath(fp, folder).replace("\\", "/"),
                "size": size,
                "ext": _ext(f),
            })
            if len(entries) > MAX_FILES_PER_SOURCE:
                raise OverflowError(f"Too many files (more than {MAX_FILES_PER_SOURCE:,})")
    return entries, subdirs


def _ae_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    info = analyze_file(entry["path"])
    return {
        "name": entry["name"],
        "rel": entry["rel"],
        "path": entry["path"],
        "size": entry["size"],
        "ext": entry["ext"],
        "kind": info["kind"],
        "year": info.get("year"),
        "major": info.get("major"),
        "patchable": bool(info.get("patchable")),
        "note": info.get("note", ""),
        "is_ae": True,
    }


def scan_source(path: str) -> Dict[str, Any]:
    """Describe one dropped file or folder (AE files analysed, the rest just counted)."""
    p = os.path.abspath(path)
    name = os.path.basename(p.rstrip("\\/")) or p
    if not os.path.exists(p):
        return {"ok": False, "name": name, "path": p, "error": "Not found"}

    if os.path.isdir(p):
        try:
            entries, _dirs = _walk(p)
        except OverflowError as e:
            return {"ok": False, "name": name, "path": p, "error": str(e)}
        items = [_ae_item(e) for e in entries if e["ext"] in AE_EXTS]
        others = [
            {
                "name": e["name"],
                "rel": e["rel"],
                "path": e["path"],
                "size": e["size"],
                "ext": e["ext"],
                "kind": "asset",
                "is_ae": False,
                "patchable": False,
                "note": "Non-AE asset (copied intact)",
            }
            for e in entries if e["ext"] not in AE_EXTS
        ]
        return {
            "ok": True,
            "kind": "folder",
            "name": name,
            "path": p,
            "items": items,
            "others": others,
            "other_count": len(others),
            "other_bytes": sum(e["size"] for e in others),
        }

    ext = _ext(name)
    if ext not in AE_EXTS:
        return {"ok": False, "name": name, "path": p, "error": "Not an After Effects file"}
    try:
        size = os.path.getsize(p)
    except OSError as e:
        return {"ok": False, "name": name, "path": p, "error": f"Can't read file ({e.strerror or e})"}
    item = _ae_item({"name": name, "rel": name, "path": p, "size": size, "ext": ext})
    item["is_ae"] = True
    return {"ok": True, "kind": "file", "name": name, "path": p, "items": [item], "others": [], "other_count": 0, "other_bytes": 0}


def scan_paths(paths: List[str]) -> List[Dict[str, Any]]:
    return [scan_source(p) for p in _dedupe_sources(paths, keep_missing=True)]


# --------------------------------------------------------------------------- #
#  Output helpers
# --------------------------------------------------------------------------- #

class Cancelled(Exception):
    pass


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def _dedupe_sources(paths: List[str], keep_missing: bool = False) -> List[str]:
    """Absolute, unique, and nothing nested inside another dropped folder."""
    seen: Dict[str, str] = {}
    for p in paths or []:
        if not p or not isinstance(p, str):
            continue
        ap = os.path.abspath(p)
        if not keep_missing and not os.path.exists(ap):
            continue
        seen.setdefault(_norm(ap), ap)
    keys = sorted(seen, key=len)
    kept: List[str] = []
    for k in keys:
        if any(k.startswith(parent + os.sep) and os.path.isdir(seen[parent]) for parent in kept):
            continue
        kept.append(k)
    keep = set(kept)
    return [v for k, v in seen.items() if k in keep]  # drop order preserved


def _unique(path: str, is_dir: bool) -> str:
    if not os.path.exists(path):
        return path
    base, ext = (path, "") if is_dir else os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _fallback_parent() -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    base = docs if os.path.isdir(docs) else os.path.expanduser("~")
    d = os.path.join(base, "Rewind AE")
    os.makedirs(d, exist_ok=True)
    return d


def _write_atomic(dest: str, data: bytes) -> None:
    tmp = dest + ".rewind-part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


def _copy_file(src: str, dst: str, cancel: threading.Event, on_bytes: Callable[[int], None]) -> None:
    tmp = dst + ".rewind-part"
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            if cancel.is_set():
                raise Cancelled()
            buf = fi.read(COPY_CHUNK)
            if not buf:
                break
            fo.write(buf)
            on_bytes(len(buf))
    try:
        shutil.copystat(src, tmp)
    except OSError:
        pass
    os.replace(tmp, dst)


def _remove_quietly(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  Conversion job
# --------------------------------------------------------------------------- #

ProgressFn = Callable[[Dict[str, Any]], None]


def convert(
    paths: List[str],
    target_year: int,
    copy_other: bool = True,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Rewind every dropped source to `target_year`.

    * a folder   -> sibling folder `<name>_AE<year>` mirroring the whole tree
    * a file     -> sibling file   `<name>_AE<year>.<ext>` (skipped if already compatible)

    Nothing is ever written over an existing file: names get " (2)", " (3)"…
    If `out_dir` is given (and exists / can be created) results go there instead of
    next to the originals.
    """
    t0 = time.time()
    cancel = cancel or threading.Event()
    if target_year not in TARGETS:
        return {"ok": False, "error": f"Unsupported target: AE {target_year}"}

    sources = _dedupe_sources(paths)
    if not sources:
        return {"ok": False, "error": "Nothing to process — the files may have been moved"}
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"Can't use the output folder ({e.strerror or e})"}

    # ---- plan ------------------------------------------------------------- #
    plans: List[Dict[str, Any]] = []
    total_weight = 0
    total_files = 0
    for src in sources:
        if os.path.isdir(src):
            try:
                entries, subdirs = _walk(src)
            except OverflowError as e:
                return {"ok": False, "error": f"{os.path.basename(src)}: {e}"}
            actions = []
            for e in entries:
                if e["ext"] in AE_EXTS:
                    a = analyze_file(e["path"])
                    needs = bool(a.get("patchable")) and a["year"] > target_year
                    e["action"] = "convert" if needs else "copy"
                    e["info"] = a
                else:
                    e["action"] = "copy" if copy_other else "skip"
                if e["action"] != "skip":
                    actions.append(e)
                    total_weight += max(e["size"], 1)
                    total_files += 1
            plans.append({"src": src, "kind": "folder", "entries": actions, "dirs": subdirs})
        else:
            ext = _ext(src)
            if ext not in AE_EXTS:
                continue
            e = {"name": os.path.basename(src), "path": src, "rel": os.path.basename(src),
                 "size": os.path.getsize(src), "ext": ext}
            a = analyze_file(src)
            e["info"] = a
            if a.get("patchable") and a["year"] > target_year:
                e["action"] = "convert"
                total_weight += max(e["size"], 1)
                total_files += 1
            else:
                e["action"] = "none"
            plans.append({"src": src, "kind": "file", "entries": [e]})

    done_weight = 0
    files_done = 0
    current = ""
    last_emit = 0.0

    def emit(force: bool = False) -> None:
        nonlocal last_emit
        if not progress:
            return
        now = time.time()
        if not force and now - last_emit < 0.05:
            return
        last_emit = now
        pct = 100 if total_weight == 0 else min(100, int(done_weight * 100 / total_weight))
        progress({"pct": pct, "current": current, "files_done": files_done, "files_total": total_files})

    def add_bytes(n: int) -> None:
        nonlocal done_weight
        done_weight += n
        emit()

    # ---- run -------------------------------------------------------------- #
    outputs: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    created: List[str] = []
    counts = {"converted": 0, "copied": 0, "failed": 0, "skipped": 0}

    def record(path: str, status: str, note: str = "", out: str = "") -> None:
        counts[status] += 1
        results.append({"path": path, "status": status, "note": note, "out": out})

    try:
        emit(True)
        for plan in plans:
            if cancel.is_set():
                raise Cancelled()
            src = plan["src"]
            parent = out_dir or os.path.dirname(src.rstrip("\\/")) or src

            if plan["kind"] == "file":
                e = plan["entries"][0]
                if e["action"] != "convert":
                    why = e["info"].get("note") if not e["info"].get("patchable") else f"Already AE {e['info']['year']}"
                    record(src, "failed" if not e["info"].get("patchable") else "skipped", why)
                    continue
                current = e["name"]
                emit(True)
                try:
                    with open(src, "rb") as f:
                        data = f.read()
                    res = patch_bytes(data, e["ext"], target_year)
                    if not res.get("ok"):
                        record(src, "failed", res.get("error", "Couldn't convert"))
                        add_bytes(max(e["size"], 1))
                        continue
                    stem, ext_ = os.path.splitext(e["name"])
                    out = None
                    for base in (parent, None):
                        base_dir = base or _fallback_parent()
                        out = _unique(os.path.join(base_dir, f"{stem}_AE{target_year}{ext_}"), False)
                        try:
                            _write_atomic(out, res["bytes"])
                            break
                        except OSError as err:
                            if base is None or err.errno == errno.ENOSPC:
                                raise
                    created.append(out)  # type: ignore[arg-type]
                    outputs.append({"source": src, "out_path": out, "kind": "file"})
                    record(src, "converted", "", out)  # type: ignore[arg-type]
                    add_bytes(max(e["size"], 1))
                except OSError as err:
                    if err.errno == errno.ENOSPC:
                        raise
                    record(src, "failed", f"I/O error: {err.strerror or err}")
                continue

            # ---- folder ---- #
            name = os.path.basename(src.rstrip("\\/")) or "Folder"
            dest_root = None
            for base in (parent, None):
                base_dir = base or _fallback_parent()
                cand = _unique(os.path.join(base_dir, f"{name}_AE{target_year}"), True)
                try:
                    os.makedirs(cand)
                    dest_root = cand
                    break
                except OSError as err:
                    if base is None:
                        raise
            assert dest_root is not None
            created.append(dest_root)
            outputs.append({"source": src, "out_path": dest_root, "kind": "folder"})
            for d in plan["dirs"]:
                os.makedirs(os.path.join(dest_root, *d.split("/")), exist_ok=True)

            for e in plan["entries"]:
                if cancel.is_set():
                    raise Cancelled()
                current = e["name"]
                dest = os.path.join(dest_root, *e["rel"].split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                emit()
                try:
                    if e["action"] == "convert":
                        with open(e["path"], "rb") as f:
                            data = f.read()
                        res = patch_bytes(data, e["ext"], target_year)
                        if res.get("ok"):
                            _write_atomic(dest, res["bytes"])
                            record(e["path"], "converted", "", dest)
                        else:
                            # keep the pack complete, but be loud about it
                            _copy_file(e["path"], dest, cancel, lambda n: None)
                            record(e["path"], "failed", res.get("error", "Couldn't convert"), dest)
                        add_bytes(max(e["size"], 1))
                    else:
                        _copy_file(e["path"], dest, cancel, add_bytes)
                        if e["size"] == 0:
                            add_bytes(1)
                        info = e.get("info")
                        if info is not None and not info.get("patchable"):
                            record(e["path"], "failed", f"Couldn't read version: {info.get('note', 'unknown')}", dest)
                        else:
                            record(e["path"], "copied", "", dest)
                    files_done += 1
                    emit()
                except Cancelled:
                    raise
                except OSError as err:
                    if err.errno == errno.ENOSPC:
                        raise
                    files_done += 1
                    record(e["path"], "failed", f"I/O error: {err.strerror or err}")

        emit(True)
    except Cancelled:
        for c in created:
            _remove_quietly(c)
        return {"ok": True, "cancelled": True, "outputs": [], "files": [], "counts": counts,
                "elapsed": round(time.time() - t0, 2), "target": target_year}
    except OSError as err:
        for c in created:
            _remove_quietly(c)
        msg = "The disk is full" if err.errno == errno.ENOSPC else f"I/O error: {err.strerror or err}"
        return {"ok": False, "error": msg}

    return {
        "ok": True,
        "cancelled": False,
        "outputs": outputs,
        "files": results,
        "counts": counts,
        "elapsed": round(time.time() - t0, 2),
        "target": target_year,
    }


# --------------------------------------------------------------------------- #
#  Show in file manager
# --------------------------------------------------------------------------- #

def reveal(path: str) -> bool:
    p = os.path.abspath(path)
    if not os.path.exists(p):
        p = os.path.dirname(p)
        if not os.path.exists(p):
            return False
    try:
        if sys.platform.startswith("win"):
            if os.path.isdir(p):
                os.startfile(p)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(f'explorer /select,"{p}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p] if os.path.isfile(p) else ["open", p])
        else:
            subprocess.Popen(["xdg-open", p if os.path.isdir(p) else os.path.dirname(p)])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Command line:  python ae_downgrader.py <files/folders…> -t 2024
# --------------------------------------------------------------------------- #

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rewind After Effects projects to an older version.")
    ap.add_argument("paths", nargs="+", help="files and/or folders")
    ap.add_argument("-t", "--target", type=int, default=2024, choices=sorted(TARGETS), help="target AE year")
    ap.add_argument("--no-other", action="store_true", help="don't copy non-AE files from folders")
    ap.add_argument("--json", action="store_true", help="print the raw result as JSON")
    args = ap.parse_args(argv)

    def show(p: Dict[str, Any]) -> None:
        sys.stderr.write(f"\r{p['pct']:3d}%  {p['current'][:60]:<60}")
        sys.stderr.flush()

    res = convert(args.paths, args.target, not args.no_other, None if args.json else show)
    if not args.json:
        sys.stderr.write("\n")
    if args.json:
        print(json.dumps(res, indent=2))
    elif not res.get("ok"):
        print("Error:", res.get("error"))
        return 1
    else:
        c = res["counts"]
        print(f"converted {c['converted']} · copied {c['copied']} · skipped {c['skipped']} · failed {c['failed']}")
        for o in res["outputs"]:
            print("→", o["out_path"])
        for f in res["files"]:
            if f["status"] == "failed":
                print("  !", f["path"], "—", f["note"])
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli())
