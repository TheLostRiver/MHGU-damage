@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ and make sure it is on PATH.
    pause
    exit /b 1
)

python main.py %*
if errorlevel 1 (
    echo.
    echo The meter exited with an error. Run "python main.py --probe" for diagnostics.
    pause
)
