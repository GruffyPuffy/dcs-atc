#!/usr/bin/env bash
# Supervisor for the ATC bot: relaunch it when the web map asks for a restart.
#
# The map server runs *inside* the bot process, so it cannot restart the bot
# itself. Instead the map's Restart button writes the requested airfield to a
# small state file (atc/.control) and the bot exits with code 75. This loop
# reads that file and relaunches start_bot.sh with the new --airfield.
#
# Only exit code 75 loops. Ctrl+C (130), a crash, or the map's Stop button
# (0) all stop the supervisor, so a failure never turns into a restart loop.
#
# Usage:
#   ./run_server.sh [extra atc_bot.py args...]
#   ATC_AIRFIELD=Gudauta ./run_server.sh --debug   # initial airfield + debug
#
# The airfield is owned by the state file once a restart has happened, so the
# supervisor's --airfield always wins over one passed on the command line.
set -uo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
atc_dir="$repo_dir/atc"
control_file="$atc_dir/.control"
start_script="$atc_dir/start_bot.sh"

# Initial airfield precedence: an explicit --airfield on the command line, then
# $ATC_AIRFIELD, then the bot's own default (Kutaisi). Once a web restart has
# happened the state file takes over (see the loop below).
airfield="${ATC_AIRFIELD:-Kutaisi}"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == "--airfield" && $((i + 1)) -lt ${#args[@]} ]]; then
    airfield="${args[i + 1]}"
  fi
done

while true; do
  debug_args=()
  if [[ -f "$control_file" ]]; then
    # Simple key=value file, so bash can read it without a JSON parser.
    requested="$(sed -n 's/^ATC_AIRFIELD=//p' "$control_file" | head -n1)"
    [[ -n "$requested" ]] && airfield="$requested"
    dbg="$(sed -n 's/^ATC_DEBUG=//p' "$control_file" | head -n1)"
    [[ "$dbg" == "1" ]] && debug_args=(--debug)
  fi

  echo "[run_server] starting bot: airfield=$airfield ${debug_args[*]:-}"
  "$start_script" "$@" --airfield "$airfield" "${debug_args[@]}"
  code=$?

  if [[ $code -eq 75 ]]; then
    echo "[run_server] restart requested (exit 75); relaunching..."
    continue
  fi
  echo "[run_server] bot exited with code $code; stopping."
  break
done
