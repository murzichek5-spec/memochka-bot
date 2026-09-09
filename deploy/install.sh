#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
echo "готово. заполни BOT_TOKEN и OPENROUTER_API_KEY в .env, затем: ./deploy/run.sh"
