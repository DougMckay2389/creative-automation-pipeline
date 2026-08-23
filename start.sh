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

# Arguments pass straight through, so the launcher does not need to learn a
# new flag every time app.py does:
#   ./start.sh              app window if Chrome/Edge is present, else a tab
#   ./start.sh --browser    force a normal browser tab
#   ./start.sh --no-open    start the server and open nothing
echo "  Starting at http://127.0.0.1:8765"
echo "  Closing the app window stops it; Ctrl-C also works."
exec $PY app.py "$@"
