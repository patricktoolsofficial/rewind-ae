# -*- mode: python ; coding: utf-8 -*-
# Build:  python -m PyInstaller --clean RewindAE.spec   (or just run build.bat)

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui.html', '.'), ('app_icon.ico', '.')],
    hiddenimports=[
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PIL', 'numpy', 'unittest', 'pydoc'],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RewindAE',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed exes are a common antivirus false-positive trigger
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app_icon.ico'],
)
