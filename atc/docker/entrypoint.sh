#!/usr/bin/env bash
# ATC bot container entrypoint: install/update the app, then run it.
#
# The image is a thin runtime; the code lives in a persistent volume. On every
# start we clone the repo (first time) or fast-forward it (after), sync the
# Python env from uv.lock, make sure the Whisper model + Piper voices are
# present, then hand off to run_server.sh (so the web Restart/Stop buttons keep
# working). This mirrors the DCS image's auto-installer: a restart = latest main.
set -euo pipefail

ATC_REPO="${ATC_REPO:-https://github.com/GruffyPuffy/dcs-atc.git}"
ATC_REF="${ATC_REF:-main}"
ATC_DIR="${ATC_DIR:-/data/atc/src}"
ATC_AIRFIELD="${ATC_AIRFIELD:-Kutaisi}"
ATC_DEBUG="${ATC_DEBUG:-0}"
# Hostnames the bot dials. In the compose network these are the service names;
# override for a different topology.
ATC_SRS_HOST="${ATC_SRS_HOST:-srs-server}"
ATC_STATE_HOST="${ATC_STATE_HOST:-dcs-server}"
ATC_STATE_PORT="${ATC_STATE_PORT:-10309}"
ATC_MAP_PORT="${ATC_MAP_PORT:-8090}"
# STT: empty model/device/compute => the bot auto-selects (GPU: large-v3,
# CPU: medium.en). Set them to force a choice.
ATC_STT_MODEL="${ATC_STT_MODEL:-}"
ATC_STT_DEVICE="${ATC_STT_DEVICE:-}"
ATC_STT_COMPUTE="${ATC_STT_COMPUTE:-}"

log() { echo "[atc-entrypoint] $*"; }

# --- 0. Drop to the host UID/GID (so volume files are not root-owned) -------
# Runs as root, fixes ownership of the data volume, then re-execs as the host
# user. PUID/PGID come from the compose .env (same convention as the DCS image).
if [[ "$(id -u)" == "0" && -n "${PUID:-}" && -n "${PGID:-}" ]]; then
    mkdir -p /data/atc
    chown -R "$PUID:$PGID" /data/atc
    exec gosu "$PUID:$PGID" "$0" "$@"
fi

# --- 1. Get/update the code -------------------------------------------------
# ATC_SKIP_UPDATE=1 uses whatever is already at ATC_DIR (e.g. a bind-mounted
# working tree) without touching git — handy for local testing.
if [[ "${ATC_SKIP_UPDATE:-0}" == "1" && -d "$ATC_DIR" ]]; then
    log "ATC_SKIP_UPDATE=1: using existing $ATC_DIR (no git update)"
elif [[ -d "$ATC_DIR/.git" ]]; then
    log "Updating $ATC_DIR ($ATC_REF)..."
    git -C "$ATC_DIR" fetch --depth 1 origin "$ATC_REF"
    git -C "$ATC_DIR" checkout -q FETCH_HEAD
else
    log "Cloning $ATC_REPO ($ATC_REF) into $ATC_DIR..."
    mkdir -p "$(dirname "$ATC_DIR")"
    git clone --depth 1 --branch "$ATC_REF" "$ATC_REPO" "$ATC_DIR"
fi

cd "$ATC_DIR/atc"

# --- 2. Python environment --------------------------------------------------
# ATC_GPU=1 also installs the CUDA runtime libs (cuBLAS/cuDNN) for GPU STT.
log "Syncing Python environment (uv)..."
if [[ "${ATC_GPU:-0}" == "1" ]]; then
    uv sync --frozen --group gpu
else
    uv sync --frozen
fi

# --- 3. Whisper model + Piper voices (persist in the volume) ----------------
# The model downloads into HF_HOME on first use; voices are fetched here.
log "Ensuring Piper voices..."
uv run python -m piper.download_voices \
    en_US-amy-medium en_US-ryan-medium en_US-lessac-medium en_US-joe-medium \
    en_GB-alan-medium \
    --download-dir voices || log "voice download failed (will retry next start)"

# --- 4. Viewer credentials ---------------------------------------------------
# The public viewer (started by run_server.sh below) needs an auth file. It is
# created here on a fresh volume only — an existing file is never touched.
# Set ATC_VIEWER_USER / ATC_VIEWER_PASSWORD in deploy/dcs/.env to choose the
# account. If unset, a random password is generated and printed ONCE to the
# log (the username defaults to "viewer") — nothing personal or guessable is
# ever baked into the image.
AUTH_FILE="$ATC_DIR/atc/.viewer-auth.json"
if [[ ! -f "$AUTH_FILE" ]]; then
    VUSER="${ATC_VIEWER_USER:-viewer}"
    VPASS="${ATC_VIEWER_PASSWORD:-}"
    if [[ -z "$VPASS" ]]; then
        VPASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
        log "No ATC_VIEWER_PASSWORD set — generated a random one (printed below)."
        log "Set ATC_VIEWER_USER / ATC_VIEWER_PASSWORD in deploy/dcs/.env to"
        log "choose your own; this log line is the only place it appears:"
        log "  viewer login -> user: $VUSER  password: $VPASS"
    fi
    log "Creating viewer auth file: $AUTH_FILE (user: $VUSER)"
    python3 - "$VUSER" "$VPASS" "$AUTH_FILE" <<'PY'
import json, sys
user, password, path = sys.argv[1:4]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"users": {user: password}}, f)
    f.write("\n")
PY
fi

# --- 5. Run -----------------------------------------------------------------
# run_server.sh loops on exit code 75 (web-driven restart) and reads the
# airfield from atc/.control, so the map's Restart/Stop buttons work.
# The SRS host comes from ATC_SRS_HOST (start_bot.sh reads it); the state host
# is passed here. The map defaults to 8090 in start_bot.sh.
export ATC_SRS_HOST ATC_STT_MODEL ATC_STT_DEVICE ATC_STT_COMPUTE
log "Starting ATC bot (airfield=$ATC_AIRFIELD, debug=$ATC_DEBUG, " \
    "stt=${ATC_STT_MODEL:-auto}/${ATC_STT_DEVICE:-auto})..."
exec ./run_server.sh \
    --state-host "$ATC_STATE_HOST" --state-port "$ATC_STATE_PORT" \
    --airfield "$ATC_AIRFIELD" \
    $([[ "$ATC_DEBUG" == "1" ]] && echo --debug)
