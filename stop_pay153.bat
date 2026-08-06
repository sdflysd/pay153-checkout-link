@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-pay153.ps1"
set "PAY153_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%PAY153_EXIT_CODE%"=="0" (
  echo Done.
) else (
  echo Failed. Check logs\flask.err.log for details.
)
if /I "%~1"=="/nopause" exit /b %PAY153_EXIT_CODE%
pause
exit /b %PAY153_EXIT_CODE%
