@echo off
rem Trustworthy Process Monitor - double-click launcher (Windows). Calls run.ps1 with the execution policy bypassed.
rem Usage: run.bat            -> install + start UI
rem        run.bat -Demo      -> also run the demo pipeline first
rem        run.bat -PullModels -> pull the local Ollama model if Ollama is installed
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo The launcher exited with code %RC%. See the messages above.
)
echo.
pause
exit /b %RC%
