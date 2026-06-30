#!/usr/bin/env bash
# Nanoclaw Remote Desktop — Restart Shortcut
# Double-click this file from the Desktop to restart the remote access service.

PROJ="$HOME/Desktop/n8n-setup/nanoclaw"
VENV="$PROJ/.venv/bin/python3"
LOG="$HOME/Library/Logs/nanoclaw-remote.log"
SERVICE="com.nanoclaw.remote"

echo "========================================="
echo "  Nanoclaw Remote Desktop — Restart"
echo "========================================="
echo ""

# ── Check if Launch Agent is registered ──────────────────────────────────────
if launchctl list 2>/dev/null | grep -q "$SERVICE"; then
    echo "► Service found. Restarting..."
    launchctl stop "$SERVICE" 2>/dev/null
    sleep 2
    launchctl start "$SERVICE"
    echo "  Started."
else
    echo "► Service not registered. Installing..."
    cd "$PROJ" || { echo "ERROR: Project not found at $PROJ"; read -p "Press Enter to close."; exit 1; }
    "$VENV" main.py remote --install
fi

echo ""
echo "► Waiting 10 seconds for tunnel to connect..."
sleep 10

echo ""
echo "► Recent log output:"
echo "-----------------------------------------"
tail -30 "$LOG" 2>/dev/null || echo "(log not found)"
echo "-----------------------------------------"
echo ""
echo "► Check your ntfy app for the new URL."
echo "  (ntfy topic: prafullaMac)"
echo ""
read -p "Press Enter to close this window."
