@echo off
chcp 65001 >nul
echo ==============================
echo   知错 AI错题本 - 一键启动
echo ==============================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv ".venv"
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
".venv\Scripts\python.exe" "server.py"
pause
