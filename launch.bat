@echo off
REM Antigravity Export - Windows double-click launcher.
REM Opens the local web UI in your default browser.

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -m antigravity_export --web
  goto :end
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python -m antigravity_export --web
  goto :end
)

echo.
echo Python is not installed, or not on PATH.
echo.
echo Install Python 3.9+ from https://www.python.org/downloads/
echo (On the first screen of the installer, tick "Add python.exe to PATH".)
echo.
echo Then run:  pip install "google-antigravity-export[gui] @ git+https://github.com/winters27/google-antigravity-export.git"
echo.
pause

:end
