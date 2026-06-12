@echo off
REM ESCApe Explorer launcher for Windows.
REM Double-click this file, or run it from a command prompt.

cd /d "%~dp0"

REM Prefer the Python launcher (py), fall back to python on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 launch.py %*
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python launch.py %*
    goto end
)

echo.
echo Python was not found on this computer.
echo Install Python 3.8 or newer from https://www.python.org/downloads/
echo During installation, tick "Add python.exe to PATH".
echo.

:end
if %errorlevel% neq 0 (
    echo.
    echo The launcher exited with an error. Review the messages above.
    pause
)
