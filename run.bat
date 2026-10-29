@echo off
REM AuditPilot one-step launcher for Windows.
REM Creates a virtual environment on first run, installs dependencies, then starts the app.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and retry.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment .venv ...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

if not exist .venv\.installed (
  echo Installing dependencies ...
  python -m pip install --upgrade pip >nul
  pip install -r requirements.txt || (pause & exit /b 1)
  echo done> .venv\.installed
)

if not exist .env copy .env.example .env >nul

echo Starting AuditPilot at http://localhost:8501
streamlit run app\Home.py --server.port 8501
