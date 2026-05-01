#!/usr/bin/env bash
# Nanoclaw — one-shot Mac setup + launcher
# Usage:  bash setup_mac.sh
set -e

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

echo -e "${CYAN}=== Nanoclaw Mac Setup ===${NC}\n"

# ── 1. Python 3 ───────────────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
  PY=$(command -v python3)
else
  echo -e "${RED}Python 3 not found.${NC}"
  if command -v brew &>/dev/null; then
    echo "Installing via Homebrew…"
    brew install python
    PY=$(command -v python3)
  else
    echo -e "${YELLOW}Install Python 3 from https://www.python.org/downloads/ then re-run this script.${NC}"
    exit 1
  fi
fi
echo -e "${GREEN}✓${NC} Python: $($PY --version)"

# ── 2. Virtual environment ────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  $PY -m venv .venv
fi
source .venv/bin/activate
echo -e "${GREEN}✓${NC} Virtualenv: .venv"

# ── 3. Dependencies ───────────────────────────────────────────────────────────
echo "Installing dependencies (this may take a minute)…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo -e "${GREEN}✓${NC} Dependencies installed"

# ── 4. .env file ─────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo -e "\n${YELLOW}No .env file found. Let's create one.${NC}"

  read -p "  OpenRouter API key (https://openrouter.ai): " OR_KEY
  read -p "  Tripo3D API key  (https://platform.tripo3d.ai): " T3D_KEY

  cat > .env <<EOF
# LLM Provider
OPENROUTER_API_KEY=${OR_KEY}

# Mesh backend
TRIPO3D_API_KEY=${T3D_KEY}

# part-gen path (CadQuery templates)
PARTGEN_PATH=${HOME}/Desktop/n8n-setup/part-gen

# Output
OUTPUT_DIR=./output
UI_PORT=7860
EOF
  echo -e "${GREEN}✓${NC} .env created"
else
  echo -e "${GREEN}✓${NC} .env already exists"
fi

# ── 5. Check part-gen ─────────────────────────────────────────────────────────
PARTGEN_PATH="${HOME}/Desktop/n8n-setup/part-gen"
if [ -f "${PARTGEN_PATH}/main.py" ]; then
  echo -e "${GREEN}✓${NC} part-gen found at ${PARTGEN_PATH}"
else
  echo -e "${YELLOW}⚠${NC}  part-gen not found at ${PARTGEN_PATH}"
  echo "   CadQuery template mode will fall back to OpenSCAD."
  echo "   (Set PARTGEN_PATH in .env if it's in a different location)"
fi

# ── 6. Launch ─────────────────────────────────────────────────────────────────
PORT=$(python3 -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get('UI_PORT','7860'))" 2>/dev/null || echo 7860)

echo -e "\n${CYAN}Starting Nanoclaw web UI on port ${PORT}…${NC}"
echo -e "Open ${GREEN}http://localhost:${PORT}${NC} in your browser\n"
echo "(Press Ctrl+C to stop)"

python3 main.py serve --port "$PORT"
