@echo off
cd /d "%~dp0"
echo Review app running at http://127.0.0.1:5000  (close this window to stop it)
start "" http://127.0.0.1:5000
.venv\Scripts\python app.py
