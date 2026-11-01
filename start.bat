@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo iPhone Charge Tray is not installed yet. Run setup.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
