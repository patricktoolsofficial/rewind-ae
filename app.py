"""
Rewind AE — desktop shell (PyWebView).

* ui.html does all rendering; this file is the bridge to the file system.
* Drag & drop: WebView2 never exposes `File.path`, so dropped paths are read
  from PyWebView's DOM `drop` event (`pywebviewFullPath`) and scanned here.
* Long jobs run on a worker thread and push progress back to the page.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

try:  # lets the logic be imported/tested without a GUI toolkit installed
    import webview  # type: ignore
except Exception:  # pragma: no cover
    webview = None  # type: ignore

import ae_downgrader as engine

APP_TITLE = "Rewind AE"
VERSION = "2.0"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "dark",          # strictly dark mode (no light mode)
    "hue": 112,              # green default
    "target": 2024,          # target AE year
    "copy_other": True,      # copy non-AE files when a folder is rewound
    "open_when_done": False,  # reveal the result automatically
    "out_mode": "beside",    # "beside" | "custom"
    "out_dir": "",
    "links": {
        "github": "https://github.com/patricktoolsofficial",
        "telegram": "https://t.me/patricktoolsofficial",
        "youtube": "https://www.youtube.com/@patricktoolsofficial",
    },
}

# native title-bar colours (Windows 11 honours them, Windows 10 just goes dark/light)
TITLEBAR = {"dark": (0x05, 0x07, 0x06, 0xF2, 0xF5, 0xF3)}
WINDOW_BG = {"dark": "#050706"}


# --------------------------------------------------------------------------- #
#  Paths / settings
# --------------------------------------------------------------------------- #

def _base_dir() -> str:
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def _config_dir() -> str:
    if sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    elif sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(root, "RewindAE")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def clean_settings(raw: Any) -> Dict[str, Any]:
    """Coerce whatever is on disk / from the page into a valid settings dict."""
    s = {**DEFAULT_SETTINGS, "links": dict(DEFAULT_SETTINGS["links"])}
    if not isinstance(raw, dict):
        return s
    s["mode"] = "dark"  # Enforce dark mode: no light mode
    try:
        # Preserve the chosen accent; fresh installs default to green
        h = raw.get("hue")
        if h is not None:
            s["hue"] = max(0, min(360, int(h)))
        else:
            s["hue"] = 112
    except (TypeError, ValueError):
        s["hue"] = 112
    try:
        t = int(raw.get("target", s["target"]))
        if t in engine.TARGETS:
            s["target"] = t
    except (TypeError, ValueError):
        pass
    for k in ("copy_other", "open_when_done"):
        if isinstance(raw.get(k), bool):
            s[k] = raw[k]
    if raw.get("out_mode") in ("beside", "custom"):
        s["out_mode"] = raw["out_mode"]
    if isinstance(raw.get("out_dir"), str):
        s["out_dir"] = raw["out_dir"]
    if isinstance(raw.get("links"), dict):
        links = dict(s["links"])
        for k, v in raw["links"].items():
            if isinstance(v, str) and v.strip():
                generic = {"github": "https://github.com", "telegram": "https://t.me", "youtube": "https://youtube.com"}
                links[k] = s["links"].get(k, v.strip()) if v.strip().rstrip("/") == generic.get(k) else v.strip()
        s["links"] = links
    return s


def load_settings() -> Dict[str, Any]:
    try:
        with open(os.path.join(_config_dir(), "settings.json"), "r", encoding="utf-8") as f:
            return clean_settings(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(s: Dict[str, Any]) -> None:
    try:
        path = os.path.join(_config_dir(), "settings.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def log_error(text: str) -> None:
    try:
        with open(os.path.join(_config_dir(), "error.log"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{text}\n")
    except OSError:
        pass


def message_box(title: str, text: str) -> None:
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
            return
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import subprocess
            cmd = f'display alert "{title}" message "{text}" as critical'
            subprocess.run(["osascript", "-e", cmd], check=False)
            return
        except Exception:
            pass
    else:
        try:
            import subprocess
            subprocess.run(["zenity", "--error", f"--title={title}", f"--text={text}"], check=False)
            return
        except Exception:
            pass
    sys.stderr.write(f"{title}: {text}\n")


# --------------------------------------------------------------------------- #
#  Windows niceties (all best-effort, never fatal)
# --------------------------------------------------------------------------- #

def _find_hwnd() -> int:
    try:
        import ctypes
        return int(ctypes.windll.user32.FindWindowW(None, APP_TITLE))
    except Exception:
        return 0


def apply_titlebar(mode: str) -> None:
    """Dark/light native title bar that matches the page."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        hwnd = _find_hwnd()
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if mode == "dark" else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old id)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), ctypes.sizeof(dark))
        r, g, b, tr, tg, tb = TITLEBAR[mode if mode in TITLEBAR else "dark"]
        caption = ctypes.c_uint(r | (g << 8) | (b << 16))
        text = ctypes.c_uint(tr | (tg << 8) | (tb << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)  # DWMWA_CAPTION_COLOR
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)     # DWMWA_TEXT_COLOR
        # nudge a non-client repaint (NOMOVE|NOSIZE|NOZORDER|NOACTIVATE|FRAMECHANGED)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    except Exception:
        pass


def round_window_corners(window: Any = None) -> None:
    """Use smooth compositor corners on Windows 11; clip only on older Windows."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        native = getattr(window, "native", None)
        hwnd = int(native.Handle.ToInt64()) if native is not None else _find_hwnd()
        if not hwnd:
            return
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        if sys.getwindowsversion().build >= 22000:
            # A custom region disables compositor rounding; remove it first.
            user32.SetWindowRgn(hwnd, None, True)
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
            return
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        dpi = user32.GetDpiForWindow(hwnd) or 96
        diameter = round(32 * dpi / 96)
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        region = gdi32.CreateRoundRectRgn(0, 0, rect.right-rect.left+1,
                                       rect.bottom-rect.top+1, diameter, diameter)
        if region and not user32.SetWindowRgn(hwnd, region, True):
            gdi32.DeleteObject(region)
    except Exception:
        log_error(traceback.format_exc())


def apply_window_icon() -> None:
    """Title-bar / taskbar icon when running from source (the .exe carries its own)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ico = os.path.join(_base_dir(), "app_icon.ico")
        hwnd = _find_hwnd()
        if not hwnd or not os.path.exists(ico):
            return
        user32 = ctypes.windll.user32
        for size, which in ((32, 1), (16, 0)):  # ICON_BIG, ICON_SMALL
            h = user32.LoadImageW(None, ico, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                user32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  JS-facing API   (public methods = callable from the page)
# --------------------------------------------------------------------------- #

class AppAPI:
    def __init__(self, settings: Dict[str, Any]) -> None:
        self._window: Any = None
        self._settings = settings
        self._lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._busy = False
        self._cancel = threading.Event()
        self._result: Optional[Dict[str, Any]] = None
        self._scans: Dict[str, List[Dict[str, Any]]] = {}

    # ---- plumbing --------------------------------------------------------- #

    def _js(self, code: str) -> None:
        w = self._window
        if w is None:
            return
        try:
            w.evaluate_js(code)
        except Exception:
            pass

    def _call(self, fn: str, payload: Any) -> None:
        self._js(f"window.RW&&RW.{fn}({json.dumps(payload)})")

    # ---- state / settings ------------------------------------------------- #

    def get_state(self) -> Dict[str, Any]:
        return {
            "settings": self._settings,
            "targets": engine.target_list(),
            "version": VERSION,
            "platform": sys.platform,
            "busy": self._busy,
        }

    def save_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._settings_lock:
            merged = dict(self._settings)
            if isinstance(patch, dict):
                merged.update(patch)
            self._settings = clean_settings(merged)
            save_settings(self._settings)
            return self._settings

    def set_theme(self, mode: str, hue: int) -> bool:
        self.save_settings({"mode": mode, "hue": hue})
        apply_titlebar(self._settings["mode"])
        return True

    # ---- adding things ---------------------------------------------------- #

    def browse(self, kind: str) -> Dict[str, Any]:
        """kind = 'files' | 'folder'. Opens the native dialog, scans, returns sources."""
        w = self._window
        if w is None or webview is None:
            return {"sources": []}
        try:
            fd = getattr(webview, "FileDialog", None)
            if kind == "folder":
                res = w.create_file_dialog(fd.FOLDER if fd else webview.FOLDER_DIALOG)
            else:
                try:
                    res = w.create_file_dialog(
                        fd.OPEN if fd else webview.OPEN_DIALOG,
                        allow_multiple=True,
                        file_types=("After Effects files (*.aep;*.aet;*.ffx;*.aepx)", "All files (*.*)"),
                    )
                except Exception:
                    # Fallback for Linux/macOS pywebview implementations that need simpler file_types
                    res = w.create_file_dialog(
                        fd.OPEN if fd else webview.OPEN_DIALOG,
                        allow_multiple=True,
                    )
        except Exception as e:
            log_error(traceback.format_exc())
            return {"sources": [], "error": f"Couldn't open the file dialog ({e})"}
        if not res:
            return {"sources": []}
        try:
            return {"sources": engine.scan_paths(list(res))}
        except Exception as e:
            log_error(traceback.format_exc())
            return {"sources": [], "error": f"Couldn't read the selected files ({e})"}

    def open_url(self, url: str) -> bool:
        """Open an external link (GitHub, Telegram, YouTube) in the system default browser."""
        import webbrowser
        if not isinstance(url, str):
            return False
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            if url.startswith("t.me") or url.startswith("github.com") or url.startswith("youtube.com"):
                url = "https://" + url
            else:
                return False
        try:
            webbrowser.open(url)
            return True
        except Exception:
            log_error(traceback.format_exc())
            return False

    def pick_output(self) -> Optional[str]:
        w = self._window
        if w is None or webview is None:
            return None
        try:
            fd = getattr(webview, "FileDialog", None)
            res = w.create_file_dialog(fd.FOLDER if fd else webview.FOLDER_DIALOG)
        except Exception:
            log_error(traceback.format_exc())
            return None
        if not res:
            return None
        self.save_settings({"out_dir": res[0], "out_mode": "custom"})
        return res[0]

    def take_scan(self, token: str) -> List[Dict[str, Any]]:
        return self._scans.pop(token, [])

    # ---- drag & drop (called by PyWebView's DOM layer) --------------------- #

    def _bind_dom(self, window: Any) -> None:
        try:
            from webview.dom import DOMEventHandler  # type: ignore
        except Exception:
            log_error("pywebview too old for DOM drop events:\n" + traceback.format_exc())
            return
        try:
            def swallow(e: Any) -> None:
                pass

            doc = window.dom.document
            doc.events.dragenter += DOMEventHandler(swallow, True, True)
            doc.events.dragstart += DOMEventHandler(swallow, True, True)
            doc.events.dragover += DOMEventHandler(swallow, True, True, debounce=500)
            doc.events.drop += DOMEventHandler(self._on_drop, True, True)
        except Exception:
            log_error("couldn't bind drop events:\n" + traceback.format_exc())

    def _on_drop(self, e: Any) -> None:
        try:
            dt = e.get("dataTransfer") or e.get("domTransfer") or {}
            files = dt.get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files if isinstance(f, dict) and f.get("pywebviewFullPath")]
        except Exception:
            log_error(traceback.format_exc())
            files, paths = [], []
        self._js(f"window.RW&&RW.dropAck({len(files)},{len(paths)})")
        if self._busy:
            self._js('window.RW&&RW.scanReady("")')  # releases the "reading" state
            return
        if not paths:
            return
        threading.Thread(target=self._scan_and_push, args=(paths,), daemon=True).start()

    def _scan_and_push(self, paths: List[str]) -> None:
        try:
            sources = engine.scan_paths(paths)
        except Exception as e:
            log_error(traceback.format_exc())
            sources = [{"ok": False, "name": "Dropped item", "path": "", "error": str(e)}]
        token = uuid.uuid4().hex
        self._scans[token] = sources
        self._js(f'window.RW&&RW.scanReady("{token}")')

    # ---- the job ---------------------------------------------------------- #

    def start(self, paths: List[str], target: int, copy_other: bool = True) -> Dict[str, Any]:
        with self._lock:
            if self._busy:
                return {"ok": False, "error": "A job is already running"}
            try:
                target = int(target)
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "error": "Unsupported target version"}
            if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p.strip() for p in paths):
                return {"ok": False, "error": "Choose files or a folder first"}
            if target not in engine.TARGETS:
                return {"ok": False, "error": "Unsupported target version"}
            self._busy = True
            self._cancel.clear()
            self._result = None
        out_dir = self._settings["out_dir"] if self._settings.get("out_mode") == "custom" and self._settings.get("out_dir") else None
        try:
            threading.Thread(target=self._run, args=(list(paths), int(target), bool(copy_other), out_dir), daemon=True).start()
        except Exception as e:
            with self._lock:
                self._busy = False
            log_error(traceback.format_exc())
            return {"ok": False, "error": f"Couldn't start conversion ({e})"}
        return {"ok": True}

    def _run(self, paths: List[str], target: int, copy_other: bool, out_dir: Optional[str]) -> None:
        try:
            res = engine.convert(paths, target, copy_other, lambda p: self._call("onProgress", p), self._cancel, out_dir)
        except Exception as e:
            log_error(traceback.format_exc())
            res = {"ok": False, "error": f"Unexpected error: {e}"}
        self._result = res
        self._busy = False
        self._js("window.RW&&RW.jobDone()")

    def get_result(self) -> Optional[Dict[str, Any]]:
        return self._result

    def cancel(self) -> bool:
        self._cancel.set()
        return True

    def reveal(self, path: str) -> bool:
        return engine.reveal(path)

    def on_closing(self) -> None:
        self._cancel.set()


# --------------------------------------------------------------------------- #
#  Boot
# --------------------------------------------------------------------------- #

def get_html(settings: Dict[str, Any]) -> str:
    path = os.path.join(_base_dir(), "ui.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return "<h1 style='font-family:sans-serif'>Rewind AE: ui.html is missing</h1>"
    boot = json.dumps({"settings": settings, "version": VERSION})
    return html.replace("/*__BOOT__*/{}", boot.replace("<", "\\u003c"), 1)


def main() -> None:
    if sys.platform.startswith("win"):
        from windows_runtime import ensure_runtime
        try:
            ensure_runtime()
        except Exception as e:
            message_box(APP_TITLE, "First-time setup needs Microsoft WebView2 and an internet connection.\n\n"
                        + str(e) + "\n\nInstall WebView2 from https://developer.microsoft.com/microsoft-edge/webview2/ and reopen Rewind AE.")
            return
    if webview is None:
        message_box(APP_TITLE, "PyWebView isn't installed.\n\nRun:  pip install -r requirements.txt")
        sys.exit(1)

    settings = load_settings()
    api = AppAPI(settings)
    win = sys.platform.startswith("win")
    window = webview.create_window(
        APP_TITLE,
        html=get_html(settings),
        js_api=api,
        width=470,
        height=760,
        min_size=(420, 600),
        resizable=False,
        frameless=True,
        easy_drag=False,
        background_color=WINDOW_BG.get(settings.get("mode", "dark"), "#050706"),
    )
    api._window = window
    try:
        window.events.closing += api.on_closing
        window.events.loaded += lambda: (apply_titlebar(api._settings["mode"]), apply_window_icon(), round_window_corners(window))
    except Exception:
        pass

    try:
        webview.start(api._bind_dom, window, debug=False, **({"gui": "edgechromium"} if win else {}))
    except Exception as e:
        log_error(traceback.format_exc())
        if win:
            message_box(
                APP_TITLE,
                "Rewind AE needs the Microsoft Edge WebView2 Runtime.\n\n"
                "Install it from:\nhttps://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                f"Details: {e}",
            )
        elif sys.platform == "darwin":
            message_box(
                APP_TITLE,
                "Could not start Rewind AE.\n\n"
                "Make sure PyWebView and PyObjC are installed:\n"
                "pip install pywebview pyobjc-core pyobjc-framework-WebKit\n\n"
                f"Details: {e}",
            )
        else:
            message_box(
                APP_TITLE,
                "Could not start Rewind AE.\n\n"
                "On Linux, ensure WebKitGTK and PyWebView dependencies are installed:\n"
                "sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.0\n"
                "pip install pywebview\n\n"
                f"Details: {e}",
            )


if __name__ == "__main__":
    main()
