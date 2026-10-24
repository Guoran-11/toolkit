@echo off
REM ============================================================
REM  ToolKit - Windows launcher
REM
REM  Usage:
REM    start.bat                  -> 127.0.0.1:5000 (opens browser)
REM    start.bat --port 8080      -> custom port
REM    start.bat --lan            -> expose on LAN (forces auth on)
REM    start.bat --no-browser     -> do not open a browser
REM ============================================================

setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title ToolKit

if not exist "run.py" goto :wrongdir

set "PY="

REM ---------- 1) project-local virtualenv (highest priority) ----------
if exist ".venv\Scripts\python.exe" (
    set "PY=%CD%\.venv\Scripts\python.exe"
    goto :found
)

REM ---------- 2) official py launcher ----------
if exist "%SystemRoot%\py.exe" (
    "%SystemRoot%\py.exe" -3 -c "pass" >nul 2>nul
    if not errorlevel 1 (
        set "PY=%SystemRoot%\py.exe"
        goto :found
    )
)

REM ---------- 3) python / python3 from PATH ----------
REM  IMPORTANT: the candidate must be *executed* to verify it.
REM  Microsoft Store drops a python.exe placeholder (AppInstallerPythonRedirector)
REM  into PATH. It exists -- so `where python` reports success -- but under a
REM  non-interactive shell it simply returns 9009. Existence != availability.
python -c "pass" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :found
)
python3 -c "pass" >nul 2>nul
if not errorlevel 1 (
    set "PY=python3"
    goto :found
)

REM ---------- 4) scan common install directories ----------
for %%D in ("%LOCALAPPDATA%\Programs\Python" "%ProgramFiles%" "C:\") do (
    for /d %%P in ("%%~D\Python3*") do (
        if exist "%%~P\python.exe" (
            "%%~P\python.exe" -c "pass" >nul 2>nul
            if not errorlevel 1 (
                set "PY=%%~P\python.exe"
                goto :found
            )
        )
    )
)

goto :nopython


:found
echo [ToolKit] interpreter : !PY!
echo [ToolKit] starting service ...
echo.
"!PY!" run.py %*
set "RC=%ERRORLEVEL%"
echo.
echo [ToolKit] process exited with code %RC%.
if not "%RC%"=="0" (
    echo.
    echo If the log above shows ModuleNotFoundError, install dependencies first:
    echo     "!PY!" -m pip install -r requirements-core.txt
)
echo.
pause
exit /b %RC%


:nopython
echo.
echo ============================================================
echo   [ERROR] No usable Python found
echo ============================================================
echo.
echo   This is a Python application and requires Python 3.11+.
echo   No official installation and no project-local .venv
echo   were found on this machine.
echo.
echo   Note: if you see python.exe under
echo     %LOCALAPPDATA%\Microsoft\WindowsApps
echo   that is a Microsoft Store placeholder, NOT a real Python.
echo.
echo   ---------------- How to fix ----------------
echo.
echo   Option 1  Install official Python (recommended)
echo     1. Open  https://www.python.org/downloads/windows/
echo     2. Download "Windows installer (64-bit)", e.g.
echo        python-3.13.x-amd64.exe
echo     3. During setup, TICK THIS BOX:
echo            [x] Add python.exe to PATH
echo     4. Re-run start.bat
echo.
echo   Option 2  China mirror (faster download)
echo     https://mirrors.huaweicloud.com/python/
echo.
echo   Option 3  Let the assistant prepare a local .venv
echo             inside this folder (no system change needed).
echo.
echo ============================================================
echo.
pause
exit /b 9009


:wrongdir
echo.
echo [ERROR] run.py not found in the current folder.
echo         Please run start.bat from the project root.
echo         Current folder: %CD%
echo.
pause
exit /b 2
