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

echo   Starting. Your browser will open at http://127.0.0.1:8765
echo   Close this window to stop the app.
echo.
%PY% app.py
pause
