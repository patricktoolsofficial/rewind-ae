@echo off
setlocal
cd /d "%~dp0"
title Rewind AE - Build
echo ========================================
echo   Rewind AE - build RewindAE.exe
echo ========================================
echo.

set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.10+ was not found. Install it from python.org and tick "Add to PATH".
    pause
    exit /b 1
)

echo [1/2] Installing dependencies...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Could not install requirements.
    pause
    exit /b 1
)

echo.
echo [2/2] Building with PyInstaller...
%PY% -m PyInstaller --clean --noconfirm RewindAE.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo Done. Your app is at: dist\RewindAE.exe
pause
