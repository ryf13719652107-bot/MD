@echo off
echo Killing old Python processes...
taskkill /F /IM python.exe >nul 2>&1
echo Starting backend...
cd /d "%~dp0backend"
call .venv\Scripts\activate
uvicorn app.main:app --reload --port 8000
pause
