@echo off
REM ---------------------------------------------------------------------
REM  FDE Social Content Agentic Automation ^& Analytics
REM  One-click start (Windows). Douglas McKay - doug@dougmckay.info
REM  Installs the three dependencies if needed, then opens the app.
REM ---------------------------------------------------------------------
cd /d "%~dp0"
echo.
echo   FDE Social Content Agentic Automation ^& Analytics
echo   Douglas McKay - doug@dougmckay.info
echo   -------------------------------------------------
echo.

set PY=
where py >nul 2>&1 && set PY=py -3
if "%PY%"=="" ( where python >nul 2>&1 && set PY=python )
if "%PY%"=="" (
  echo   Python 3.10+ was not found.
  echo   Install it from https://www.python.org/downloads/ and run this again.
  echo.
  pause
  exit /b 1
)

echo   Checking dependencies...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo   Could not install dependencies. See the message above.
  pause
  exit /b 1
)

REM  Arguments are passed straight through, so the launcher does not need to
REM  learn a new flag every time app.py does:
REM    start.bat              app window if Chrome/Edge is present, else a tab
REM    start.bat --browser    force a normal browser tab
REM    start.bat --no-open    start the server and open nothing
echo   Starting at http://127.0.0.1:8765
echo   Closing the app window stops it.
echo.
%PY% app.py %*
pause
