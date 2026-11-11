@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Python not found, installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

echo Using: %PY%
%PY% --version
%PY% -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q
.venv\Scripts\python -m unittest discover tests
echo.
echo Setup finished.
timeout /t 20
