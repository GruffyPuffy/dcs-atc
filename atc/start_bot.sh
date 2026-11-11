#!/usr/bin/env bash
# Start the ATC trainer bot: listen, STT, rules-based replies, TTS over SRS.
# Frequency, tower name and runway come from airspace.json (per --airfield).
# The live map view is served by default on port 8090 (open http://<host>:8090/).
# Usage: ./start_bot.sh [extra atc_bot.py args...]
# Examples:
#   ./start_bot.sh                          # defaults: airfield Kutaisi (270.000 AM), map on :8090
#   ./start_bot.sh --airfield Batumi        # a different airfield from airspace.json
#   ./start_bot.sh --freq 124.0             # override the frequency
#   ./start_bot.sh --speech-rate 0.6        # faster TTS voice
#   ./start_bot.sh --gain 3                 # boost quiet mic audio
#   ./start_bot.sh --tx-volume 0.6          # quieten loud ATC voices (default)
#   ./start_bot.sh --map-port 9000          # move the map to another port
#   ./start_bot.sh --map-port 0             # disable the map
#   ./start_bot.sh --stt-server             # start local Speaches + use API STT
#   ./start_bot.sh --stt-server http://127.0.0.1:18000  # use an existing STT server
#   ./start_bot.sh --debug                  # + verbose DBG log + in-game comms mirror
#   ./start_bot.sh --no-debrief             # skip the debrief file (saved by default)
#   ./start_bot.sh --replay debrief/tracks_gudauta_20261010-114445.json  # review a saved sortie
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
atc_dir="$repo_dir/atc"

# `--stt-server [BASE_URL]` is a launcher option, not an atc_bot.py option.
# Without a URL, start the local Speaches container; with one, use that server.
start_stt_server=0
stt_server_url=""
bot_args=()
while (($#)); do
  if [[ "$1" == "--stt-server" ]]; then
    start_stt_server=1
    shift
    if (($#)) && [[ "$1" == http://* || "$1" == https://* ]]; then
      stt_server_url="${1%/}"
      shift
    fi
  else
    bot_args+=("$1")
    shift
  fi
done
set -- "${bot_args[@]}"

if [[ "$start_stt_server" == "1" ]]; then
  if [[ -z "$stt_server_url" ]]; then
    "$repo_dir/scripts/stt-server.sh" start
    stt_server_url="http://127.0.0.1:18000/v1"
  elif [[ "$stt_server_url" != */v1 ]]; then
    stt_server_url="${stt_server_url}/v1"
  fi
  export ATC_STT_BACKEND=api
  export ATC_STT_BASE_URL="$stt_server_url"
  export ATC_STT_API_MODEL="${ATC_STT_API_MODEL:-Systran/faster-whisper-large-v3}"
fi

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

# Keep DEBUG marks attributable when running from a host checkout as well as
# from the Docker entrypoint (which already exports these values).
if command -v git >/dev/null 2>&1; then
  ATC_GIT_SHA="${ATC_GIT_SHA:-$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || true)}"
  ATC_VERSION="${ATC_VERSION:-$(git -C "$repo_dir" describe --tags --exact-match HEAD 2>/dev/null \
    || git -C "$repo_dir" rev-parse --short=12 HEAD 2>/dev/null || true)}"
fi
export ATC_GIT_SHA="${ATC_GIT_SHA:-unknown}"
export ATC_VERSION="${ATC_VERSION:-dev}"

# Whisper STT: by default the bot auto-selects (large-v3 on a GPU,
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

echo "Starting ATC bot ($ATC_VERSION, ${ATC_GIT_SHA:0:12}; log: /tmp/atc_log.txt) — Ctrl+C to stop."
if [[ "${ATC_STT_BACKEND:-local}" == "api" ]]; then
  echo "STT backend: API at ${ATC_STT_BASE_URL:-<missing ATC_STT_BASE_URL>}"
fi
exec "$uv_bin" run atc_bot.py --host "$srs_host" --eam atc --log /tmp/atc_log.txt \
  "${map_args[@]}" "${stt_args[@]}" "$@"
