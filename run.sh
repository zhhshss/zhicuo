#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PYTHON_BIN="python3"
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
else
  "$PYTHON_BIN" -m venv ".venv"
  PYTHON_BIN=".venv/bin/python"
fi
"$PYTHON_BIN" -m pip install -r "requirements.txt"
"$PYTHON_BIN" "server.py"
