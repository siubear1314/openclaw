#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Optional sync step:
# If a sibling folder named admissions-interview exists, copy latest code into current folder.
# This helps when you edit code in admissions-interview but run from admissions-interviewer.
SOURCE_DIR="$(cd .. && pwd)/admissions-interview"
TARGET_DIR="$(pwd)"

if [ -d "$SOURCE_DIR" ]; then
  echo "Syncing latest code from: $SOURCE_DIR"
  # Prefer rsync for clean incremental sync; fall back to cp if rsync is unavailable.
  if command -v rsync >/dev/null 2>&1; then
    rsync -av --delete \
      --exclude '.venv' \
      --exclude '.env' \
      --exclude 'interviews.db' \
      --exclude '__pycache__' \
      --exclude '.git' \
      "$SOURCE_DIR/" "$TARGET_DIR/"
  else
    echo "rsync not found; using cp fallback"
    cp -R "$SOURCE_DIR"/* "$TARGET_DIR"/
  fi
else
  echo "No admissions-interview source folder found; skipping sync."
fi

if [ ! -f ".env" ]; then
  echo "Missing .env file in $(pwd)"
  echo "Create it with DISCORD_TOKEN, OPENAI_API_KEY, GUILD_ID, etc."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -U discord.py openai >/dev/null

set -a
source .env
set +a

python bot.py
