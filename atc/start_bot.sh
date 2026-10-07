#!/usr/bin/env bash
# Start the ATC trainer bot: listen, STT, rules-based replies, TTS over SRS.
# Usage: ./start_bot.sh [extra atc_bot.py args...]
# Examples:
#   ./start_bot.sh                          # defaults: 251.000 AM, EAM "atc"
#   ./start_bot.sh --freq 124.0             # different frequency
#   ./start_bot.sh --speech-rate 0.6        # faster TTS voice
#   ./start_bot.sh --gain 3                 # boost quiet mic audio
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
atc_dir="$repo_dir/atc"
uv_bin="$HOME/.local/bin/uv"

if [[ ! -x "$uv_bin" ]]; then
  echo "uv not found at $uv_bin — install it first:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

cd "$atc_dir"
mkdir -p "$atc_dir"

# fresh session: clear last run's log and captured audio
rm -f /tmp/atc_log.txt
rm -rf /tmp/atc_audio

echo "Starting ATC bot (log: /tmp/atc_log.txt) — Ctrl+C to stop."
exec "$uv_bin" run atc_bot.py --host 127.0.0.1 --freq 251.0 --eam atc --log /tmp/atc_log.txt "$@"
