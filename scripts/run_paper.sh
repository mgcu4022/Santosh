#!/usr/bin/env bash
# Start today's paper-trading session (Linux / macOS). Schedule with cron, e.g.:
#   crontab -e
#   CRON_TZ=Asia/Kolkata
#   0 9 * * 1-5  /full/path/to/Santosh/scripts/run_paper.sh config/my.yaml
# The bot exits by itself on weekends/holidays and after 15:30.
set -uo pipefail
cd "$(dirname "$0")/.."
CONFIG="${1:-config/my.yaml}"
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
mkdir -p runs/logs
LOG="runs/logs/$(date +%F).log"
export DISPLAY="${DISPLAY:-:0}"      # lets cron open the browser for the Upstox login
python -m tradebot paper -c "$CONFIG" --login -v >> "$LOG" 2>&1
python -m tradebot report --telegram >> "$LOG" 2>&1 || python -m tradebot report >> "$LOG" 2>&1
