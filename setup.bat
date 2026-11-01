@echo off
setlocal
cd /d "%~dp0"
title iPhone Charge Tray - Setup

echo ======================================
echo   iPhone Charge Tray - Setup
echo ======================================
echo.
echo This app uses Python 3.12 because pylzss does not yet provide a
echo compatible wheel for every Python version. Python 3.14 may try to
echo compile pylzss and fail with missing C headers such as io.h.
echo.

REM Prefer Python 3.12. The project pins pylzss 0.3.4, which has a CPython 3.12 Windows wheel.
py -3.12 --version >nul 2>&1
if errorlevel 1 goto :need_python

REM Detect and replace a virtual environment made with an incompatible Python version.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
  if errorlevel 1 (
    echo Removing the existing virtual environment because it uses a different Python version...
    rmdir /s /q ".venv"
    if exist ".venv" (
      echo ERROR: Could not remove .venv. Close any running app process and try again.
      pause
      exit /b 1
    )
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a Python 3.12 virtual environment...
  py -3.12 -m venv .venv
  if errorlevel 1 goto :failed
)

echo.
echo Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo.
echo Installing iPhone Charge Tray requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Verifying the key packages...
".venv\Scripts\python.exe" -c "import pymobiledevice3, lzss, pystray, PIL; print('All key packages imported successfully.')"
if errorlevel 1 goto :failed

echo.
echo Setup complete. Double-click start.bat to launch the tray app.
echo Keep your iPhone unlocked and accept Trust This Computer if asked.
pause
exit /b 0

:need_python
echo ERROR: Python 3.12 was not found by the Windows Python launcher.
echo Install 64-bit Python 3.12 from:
echo https://www.python.org/downloads/release/python-31210/
echo Keep Python 3.14 installed if you use it for other projects; it is not necessary to uninstall it.
echo After installation, run setup.bat again.
pause
exit /b 1

:failed
echo.
echo Setup failed. Copy the full error text before closing this window.
echo This version pins pylzss 0.3.4 to use a published CPython 3.12 Windows wheel.
pause
exit /b 1
