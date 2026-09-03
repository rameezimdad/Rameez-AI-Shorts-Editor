#!/usr/bin/env bash
# Developed by Mohammad Rameez Imdad (Rameez Scripts)
# WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
# YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)
# One-shot launcher for WSL / Linux / macOS: venv + deps + assets + server.
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "ffmpeg not found on PATH - install it first (see README)"; exit 1; }
# WSL: reuse the venv outside OneDrive if it exists, else create .venv here
VENV="${VENV:-$([ -d "$HOME/.venvs/ai-shorts" ] && echo "$HOME/.venvs/ai-shorts" || echo .venv)}"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
"$VENV/bin/python" scripts/setup_assets.py
echo; echo "  Open http://127.0.0.1:8000   (Ctrl+C stops the server)"; echo
exec "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8000
