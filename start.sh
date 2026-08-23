#!/usr/bin/env bash
# ---------------------------------------------------------------------
#  FDE Social Content Agentic Automation & Analytics
#  One-click start (macOS / Linux). Douglas McKay - doug@dougmckay.info
# ---------------------------------------------------------------------
set -e
cd "$(dirname "$0")"

PY=""
command -v python3 >/dev/null 2>&1 && PY=python3
[ -z "$PY" ] && command -v python >/dev/null 2>&1 && PY=python
if [ -z "$PY" ]; then
  echo "Python 3.10+ was not found. Install it and run this again."
  exit 1
fi

echo "  Checking dependencies..."
$PY -m pip install -q --disable-pip-version-check -r requirements.txt

echo "  Starting. Your browser will open at http://127.0.0.1:8765"
echo "  Press Ctrl-C to stop."
exec $PY app.py
