@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
.venv\Scripts\python pipeline.py
timeout /t 20
