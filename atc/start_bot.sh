#!/usr/bin/env bash
# Start the ATC trainer bot: listen, STT, rules-based replies, TTS over SRS.
# Frequency, tower name and runway come from airspace.json (per --airfield).
# The live map view is served by default on port 8090 (open http://<host>:8090/).
# Usage: ./start_bot.sh [extra atc_bot.py args...]
# Examples:
#   ./start_bot.sh                          # defaults: airfield Kutaisi (263.000 AM), map on :8090
#   ./start_bot.sh --airfield Batumi        # a different airfield from airspace.json
#   ./start_bot.sh --freq 124.0             # override the frequency
#   ./start_bot.sh --speech-rate 0.6        # faster TTS voice
#   ./start_bot.sh --gain 3                 # boost quiet mic audio
#   ./start_bot.sh --tx-volume 0.4          # quieten loud ATC voices (default)
#   ./start_bot.sh --map-port 9000          # move the map to another port
#   ./start_bot.sh --map-port 0             # disable the map
#   ./start_bot.sh --debug                  # + save a debrief to atc/debrief/tracks_<field>_<time>.json
#   ./start_bot.sh --replay debrief/tracks_gudauta_20261010-114445.json  # review a saved sortie
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
atc_dir="$repo_dir/atc"

# Prefer uv on PATH (e.g. inside the container image); fall back to the usual
# per-user install location.
uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
  uv_bin="$HOME/.local/bin/uv"
fi

if [[ -z "$uv_bin" ]]; then
  echo "uv not found — install it first:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

cd "$atc_dir"
mkdir -p "$atc_dir"

# fresh session: clear last run's log
# (debrief trail files under atc/debrief/ are kept for later replay)
rm -f /tmp/atc_log.txt

# Serve the live map by default, unless the caller already set --map-port.
map_args=()
if [[ " $* " != *" --map-port "* ]]; then
  map_args=(--map-port 8090)
fi

# SRS host: default to localhost (host dev); the container sets ATC_SRS_HOST to
# the SRS service name. An explicit --host on the command line still wins.
srs_host="${ATC_SRS_HOST:-127.0.0.1}"

# Whisper STT: by default the bot auto-selects (large-v3-turbo on a GPU,
# medium.en on CPU). Override with ATC_STT_MODEL / ATC_STT_DEVICE /
# ATC_STT_COMPUTE (e.g. ATC_STT_MODEL=small.en on a low-power host). An explicit
# --stt-* on the command line still wins.
stt_args=()
if [[ " $* " != *" --stt-model "* && -n "${ATC_STT_MODEL:-}" ]]; then
  stt_args+=(--stt-model "$ATC_STT_MODEL")
fi
if [[ " $* " != *" --stt-device "* && -n "${ATC_STT_DEVICE:-}" ]]; then
  stt_args+=(--stt-device "$ATC_STT_DEVICE")
fi
if [[ " $* " != *" --stt-compute "* && -n "${ATC_STT_COMPUTE:-}" ]]; then
  stt_args+=(--stt-compute "$ATC_STT_COMPUTE")
fi

echo "Starting ATC bot (log: /tmp/atc_log.txt) — Ctrl+C to stop."
exec "$uv_bin" run atc_bot.py --host "$srs_host" --eam atc --log /tmp/atc_log.txt \
  "${map_args[@]}" "${stt_args[@]}" "$@"
