#!/bin/bash
# Daily Financial News Brief — Cron Runner
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Load .env if present
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Check required keys
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "[ERROR] OPENROUTER_API_KEY not set"
    exit 1
fi

echo "=== Daily Financial Brief — $(date -I) ==="
python daily_brief.py 2>&1 | tee "logs/$(date -I).log"