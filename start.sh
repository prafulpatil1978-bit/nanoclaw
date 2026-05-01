#!/usr/bin/env bash
# Quick-start after setup_mac.sh has been run once.
# Usage:  bash start.sh
set -e

if [ ! -d ".venv" ]; then
  echo "Run setup_mac.sh first."
  exit 1
fi

source .venv/bin/activate

PORT=$(python3 -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get('UI_PORT','7860'))" 2>/dev/null || echo 7860)

echo "Starting Nanoclaw on http://localhost:${PORT}"
python3 main.py serve --port "$PORT"
