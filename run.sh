#!/usr/bin/env bash
# AuditPilot one-step launcher for macOS / Linux.
# Creates a virtual environment on first run, installs dependencies, then starts the app.
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ not found. Install it from https://www.python.org/downloads/ and retry." >&2
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required (found $($PY --version))." >&2; exit 1; }

if [ ! -d .venv ]; then
  echo "Creating virtual environment (.venv)…"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing dependencies…"
  python -m pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
  touch .venv/.installed
fi

[ -f .env ] || cp .env.example .env

echo "Starting AuditPilot at http://localhost:${PORT:-8501}"
exec streamlit run app/Home.py --server.port "${PORT:-8501}"
