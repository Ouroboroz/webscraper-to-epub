#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/hbyang/Workspace/epub"
PYTHON="/home/hbyang/miniconda3/envs/epub/bin/python"
LOCK_FILE="$REPO_DIR/.update_check.lock"
LOG_FILE="$REPO_DIR/logs/update_check_$(date +%Y%m%d).log"

mkdir -p "$REPO_DIR/logs"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "$(date -Iseconds) update check already running, skipping this trigger" >> "$LOG_FILE"
  exit 0
fi

cd "$REPO_DIR"
{
  echo "===== $(date -Iseconds) starting update check ====="
  "$PYTHON" -m epub_scraper.update check
  echo "===== $(date -Iseconds) finished ====="
} >> "$LOG_FILE" 2>&1
