@echo off
setlocal
cd /d "%~dp0"
title Rewind AE

set "PY=python"
where python >nul 2>nul || set "PY=py -3"

%PY% -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo Installing pywebview (first run only)...
    %PY% -m pip install "pywebview>=5.1"
    if errorlevel 1 (
        echo [ERROR] Could not install pywebview.
        pause
        exit /b 1
    )
)

%PY% app.py
if errorlevel 1 pause
