#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

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
