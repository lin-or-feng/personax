@echo off
cd /d "%~dp0"
if not exist "%~dp0.tmp" mkdir "%~dp0.tmp"
set "STARTUP_LOG=%~dp0.tmp\start-ui.log"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_ui.ps1" > "%STARTUP_LOG%" 2>&1
set "STARTUP_EXIT=%ERRORLEVEL%"
if not "%STARTUP_EXIT%"=="0" (
  echo.
  echo PersonaX startup failed. Log: %STARTUP_LOG%
  type "%STARTUP_LOG%"
  pause
)
exit /b %STARTUP_EXIT%
