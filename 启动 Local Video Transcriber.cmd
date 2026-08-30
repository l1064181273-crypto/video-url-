@echo off
setlocal

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "LVT_EXIT_CODE=%ERRORLEVEL%"

if not "%LVT_EXIT_CODE%"=="0" (
  echo.
  echo Local Video Transcriber could not finish installation.
  pause
)

exit /b %LVT_EXIT_CODE%
