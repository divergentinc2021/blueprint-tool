@echo off
REM Double-click this to install the Blueprint add-on into Blender.
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 install_addon.py %*
    goto done
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python install_addon.py %*
    goto done
)

echo Python 3 not found. Install it from https://www.python.org/downloads/
echo Tick "Add python.exe to PATH" in the installer.

:done
echo.
pause
