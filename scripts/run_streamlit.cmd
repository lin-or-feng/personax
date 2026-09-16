@echo off
setlocal
cd /d "%~dp0.."
set "PORT=%~1"
if not defined PORT set "PORT=8501"
if not exist ".tmp" mkdir ".tmp"
> ".tmp\streamlit-%PORT%.stdout.log" echo [%date% %time%] Starting PersonaX on port %PORT%
> ".tmp\streamlit-%PORT%.stderr.log" echo.
".venv\Scripts\python.exe" -m streamlit run "app.py" --server.address=127.0.0.1 --server.port=%PORT% --server.headless=true 1>> ".tmp\streamlit-%PORT%.stdout.log" 2>> ".tmp\streamlit-%PORT%.stderr.log"
set "EXIT_CODE=%ERRORLEVEL%"
>> ".tmp\streamlit-%PORT%.stderr.log" echo [%date% %time%] Streamlit exited with code %EXIT_CODE%
exit /b %EXIT_CODE%
