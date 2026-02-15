#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Always copy latest bot.py from source path into current run directory.
cp /Users/rosanna/.openclaw/workspace/admissions-interviewer/bot.py ./bot.py

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
pip install -U discord.py openai requests >/dev/null

set -a
source .env
set +a

python bot.py
