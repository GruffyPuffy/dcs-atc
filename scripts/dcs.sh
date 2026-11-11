#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$repo_dir/deploy/dcs"
env_file="$compose_dir/.env"
data_root=/data/dcs-atc
saved_games="$data_root/config/.wine/drive_c/users/abc/Saved Games/DCS.dcs_serverrelease"
expected_uuid=d15d08d9-8da9-4f2a-8c6d-530907121096

usage() {
  cat <<'EOF'
Usage: ./scripts/dcs.sh <command>

  install   Prepare configuration and start the container (first installation)
  start     Start the container and apply Compose changes
  stop      Stop the container without deleting its data
  status    Show container status
  health    Read-only health summary (containers, bot mode, map, bridge)
  debriefs  List debrief and DEBUG marker files saved in the bot container
  debrief   Download a debrief (name, or 'all') to atc/debrief/ on the host
  logs      Follow recent container logs (Ctrl+C to exit)
srs-autoconnect  Install the SRS auto-connect announcer hook (port-only by default)
  config    Prepare configuration and validate Compose without starting
  srs-logs  Follow recent SRS server logs (Ctrl+C to exit)
  srs-status Show connected SRS clients (HTTP status API)
  atc-up [REF] Start the ATC bot (latest release tag by default; REF may be a tag or SHA)
  atc-up-gpu [REF] Start the ATC bot with GPU (needs nvidia-container-toolkit)
  atc-up-stt-server [REF] Start Speaches on GPU and the bot using API STT
  stt-down  Stop and remove Speaches (keeps its downloaded model cache)
  stt-status Show Speaches container and API status
  atc-down  Stop the ATC bot container
  atc-logs  Follow the ATC bot container logs (Ctrl+C to exit)
  atc-update [REF] Recreate the bot (keeps current version if omitted; keeps GPU mode)
  help      Show this help
EOF
}

compose() {
  prepare_env
  docker compose --project-directory "$compose_dir" -f "$compose_dir/docker-compose.yml" "$@"
}

# Compose with the ATC bot service added (docker-compose.atc.yml override).
compose_atc() {
  prepare_env
  docker compose --project-directory "$compose_dir" \
    -f "$compose_dir/docker-compose.yml" \
    -f "$compose_dir/docker-compose.atc.yml" "$@"
}

# Compose with the ATC bot + GPU override (needs nvidia-container-toolkit).
compose_atc_gpu() {
  prepare_env
  docker compose --project-directory "$compose_dir" \
    -f "$compose_dir/docker-compose.yml" \
    -f "$compose_dir/docker-compose.atc.yml" \
    -f "$compose_dir/docker-compose.gpu.yml" "$@"
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Docker Engine and the Compose plugin are required. See deploy/dcs/README.md." >&2
    exit 1
  fi
}

# Preflight for GPU mode: the driver must be present and Docker must have the
# NVIDIA runtime registered (from the NVIDIA Container Toolkit). Without this
# the container fails with a cryptic "could not select device driver" error.
require_gpu() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "No NVIDIA driver found (nvidia-smi missing). Install the driver first." >&2
    exit 1
  fi
  if ! docker info 2>/dev/null | grep -qi 'nvidia'; then
    echo "Docker has no NVIDIA runtime registered. Install the NVIDIA Container Toolkit:" >&2
    echo "  sudo ./scripts/install-nvidia-container.sh" >&2
    exit 1
  fi
}

# True if the running bot container was started in GPU mode (ATC_GPU=1).
atc_gpu_running() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' dcs-atc-bot 2>/dev/null \
    | grep -q '^ATC_GPU=1'
}

atc_api_running() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' dcs-atc-bot 2>/dev/null \
    | grep -q '^ATC_STT_BACKEND=api$'
}

atc_env_value() {
  local name="$1"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' dcs-atc-bot 2>/dev/null \
    | sed -n "s/^${name}=//p" | head -n 1
}

latest_atc_tag() {
  local repo="${ATC_REPO:-https://github.com/GruffyPuffy/dcs-atc.git}"
  local tag
  tag="$(git ls-remote --tags --refs "$repo" 'refs/tags/v[0-9]*' \
    | awk '{sub("refs/tags/", "", $2); print $2}' | sort -V | tail -n 1)"
  if [[ -z "$tag" ]]; then
    echo "No release tag found at $repo (expected tags like v2026.10.26.1)." >&2
    return 1
  fi
  printf '%s\n' "$tag"
}

current_atc_sha() {
  docker exec dcs-atc-bot git -c safe.directory=/data/atc/src \
    -C /data/atc/src rev-parse HEAD 2>/dev/null
}

# Read-only health summary: containers, bot mode, map, SRS clients, DCS bridge.
health() {
  require_docker

  echo "== Containers =="
  local c state
  for c in dcs-atc-server dcs-atc-srs dcs-atc-bot; do
    state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    printf '  %-16s %s\n' "$c" "$state"
  done

  echo "== ATC bot =="
  if docker inspect dcs-atc-bot >/dev/null 2>&1; then
    if atc_gpu_running; then
      echo "  mode             GPU (ATC_GPU=1)"
    else
      echo "  mode             CPU"
    fi
    local stt
    stt="$(docker logs dcs-atc-bot 2>&1 \
      | grep -o "Loading Whisper '[^']*' on [a-z]* ([a-z0-9]*)" | tail -1)"
    [[ -n "$stt" ]] && echo "  stt              ${stt#Loading Whisper }"
    if curl -fsS -o /dev/null "http://127.0.0.1:8090/" 2>/dev/null; then
      echo "  map              http://127.0.0.1:8090/  reachable"
    else
      echo "  map              not reachable"
    fi
  else
    echo "  (not running)"
  fi

  echo "== DCS state bridge =="
  if ss -ltn 2>/dev/null | grep -q ':10309 '; then
    echo "  port 10309       listening"
  else
    echo "  port 10309       not listening"
  fi
}

require_data_disk() {
  if [[ "$(findmnt -n -o UUID --target /data 2>/dev/null || true)" != "$expected_uuid" ]]; then
    echo "/data must be the expected data-disk mount before continuing." >&2
    exit 1
  fi
  if [[ ! -w /data ]]; then
    echo "/data is not writable by $(id -un). Check its ownership on the host." >&2
    exit 1
  fi
}

prepare_env() {
  python3 - "$env_file" "$(id -u)" "$(id -g)" "$(id -un)" <<'PY'
import os
import secrets
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
uid, gid, username = sys.argv[2:]
old = path.read_text() if path.exists() else ""
lines = old.splitlines()
values = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in lines if "=" in line}
password = values.get("WEBTOP_PASSWORD", "")
generated = not password or password == "REPLACE_WITH_A_LONG_UNIQUE_PASSWORD"
if generated:
    password = secrets.token_urlsafe(24)

replacements = {"PUID": uid, "PGID": gid, "WEBTOP_USER": username, "WEBTOP_PASSWORD": password}
seen = set()
updated = []
for line in lines:
    key = line.split("=", 1)[0]
    if key in replacements:
        if key not in seen:
            updated.append(f"{key}={replacements[key]}")
            seen.add(key)
    else:
        updated.append(line)
for key, value in replacements.items():
    if key not in seen:
        updated.append(f"{key}={value}")
new = "\n".join(updated) + "\n"
if new != old:
    fd, temp_name = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(new)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
else:
    path.chmod(0o600)
if generated:
    print(f"Generated a Webtop password in {path}; keep this file private.")
PY
}

prepare() {
  require_data_disk
  require_docker
  mkdir -p "$data_root/config"
  if [[ ! -w "$data_root/config" ]]; then
    echo "$data_root/config is not writable by $(id -un)." >&2
    exit 1
  fi
  compose config --quiet
}

deploy_state_bridge() {
  deploy_bridge_from "$repo_dir/bridge"
}

deploy_bridge_from() {
  local source_dir="$1"
  BRIDGE_HOOK_CHANGED=0
  deploy_hook "$source_dir/dcs_state_hook.lua" "dcs_state_hook.lua" restart
  deploy_hook "$source_dir/dcs_state_body.lua" "dcs_state_body.lua" hot
}

# For bot start/update, deploy the bridge files from the exact same remote
# commit that the entrypoint will check out. Keep the scratch checkout private
# to this invocation and never use a moving branch twice.
fetch_bridge_for_ref() {
  local ref="$1"
  local remote="${ATC_REPO:-https://github.com/GruffyPuffy/dcs-atc.git}"
  BRIDGE_TMP="$(mktemp -d)"
  git -C "$BRIDGE_TMP" init -q
  git -C "$BRIDGE_TMP" remote add origin "$remote"
  if ! git -C "$BRIDGE_TMP" fetch --quiet --depth 1 origin "$ref"; then
    echo "Could not fetch ATC ref '$ref' from $remote; bridge files were not changed." >&2
    rm -rf "$BRIDGE_TMP"
    BRIDGE_TMP=""
    return 1
  fi
  ATC_RESOLVED_SHA="$(git -C "$BRIDGE_TMP" rev-parse FETCH_HEAD)"
  mkdir -p "$BRIDGE_TMP/checkout"
  git -C "$BRIDGE_TMP" archive FETCH_HEAD bridge/dcs_state_hook.lua bridge/dcs_state_body.lua \
    | tar -x -C "$BRIDGE_TMP/checkout"
  deploy_bridge_from "$BRIDGE_TMP/checkout/bridge"
}

cleanup_bridge_tmp() {
  if [[ -n "${BRIDGE_TMP:-}" && -d "$BRIDGE_TMP" ]]; then
    rm -rf "$BRIDGE_TMP"
  fi
}
trap cleanup_bridge_tmp EXIT

# DCS reads the hook file only when its process starts. The body is re-read on
# every bridge request. Never restart DCS here; report the exact consequence.
is_dcs_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' dcs-atc-server 2>/dev/null || true)" == "true" ]]
}

deploy_srs_autoconnect() {
  require_data_disk
  # By default the hook announces the PORT ONLY ("SRS Running on 5002"); the SRS
  # client then reuses the host it used to reach DCS, so it works for LAN *and*
  # internet clients with no config. Set SRS_HOST to force an explicit address
  # (e.g. a public hostname): SRS_HOST=srs.example.com ./scripts/dcs.sh srs-autoconnect
  local srs_host="${SRS_HOST:-}"
  local rendered
  rendered="$(mktemp)"
  sed "s/__SRS_HOST__/$srs_host/" "$repo_dir/bridge/srs_autoconnect.lua" > "$rendered"
  deploy_hook "$rendered" "srs_autoconnect.lua"
  rm -f "$rendered"
  if [[ -n "$srs_host" ]]; then
    echo "SRS announcer: explicit host $srs_host:5002"
  else
    echo "SRS announcer: port-only (clients reuse the DCS host; LAN + internet)"
  fi
}

deploy_hook() {
  require_data_disk
  local hooks="$saved_games/Scripts/Hooks"
  local source="$1"
  local filename="${2:-srs_autoconnect.lua}"
  local reload_kind="${3:-restart}"
  local destination="$hooks/$filename"
  mkdir -p "$hooks"
  if [[ ! -w "$hooks" ]]; then
    echo "DCS Saved Games Hooks directory is not writable." >&2
    exit 1
  fi
  if [[ -f "$destination" ]] && cmp -s "$source" "$destination"; then
    return
  else
    install -m 644 "$source" "$destination"
    echo "Deployed: $destination"
  fi
  if [[ "$filename" == "dcs_state_hook.lua" ]]; then
    BRIDGE_HOOK_CHANGED=1
  fi
  if [[ "$reload_kind" == "hot" ]]; then
    if is_dcs_running && [[ "${BRIDGE_HOOK_CHANGED:-0}" == "1" ]]; then
      echo "$filename updated; it will be read after the changed hook is loaded by restarting DCS."
    elif is_dcs_running; then
      echo "$filename updated; DCS will use it on the next bridge request."
    else
      echo "$filename updated; it will be read on the next DCS bridge request."
    fi
  elif is_dcs_running; then
    echo "$filename changed; restart the DCS process to load it."
  else
    echo "$filename changed; it will load the next time DCS starts."
  fi
}

command="${1:-help}"
# Commands with positional arguments are limited to these explicit cases.
if [[ "$#" -gt 1 && "$command" != "debrief" \
      && "$command" != "atc-up" && "$command" != "atc-up-gpu" \
      && "$command" != "atc-up-stt-server" \
      && "$command" != "atc-update" ]]; then
  usage >&2
  exit 2
fi

case "$command" in
  help|-h|--help)
    usage
    ;;
  config)
    prepare
    echo "Configuration valid. Persistent DCS data: $data_root/config"
    ;;
  install|start)
    prepare
    require_docker
    # If a bot checkout exists, deploy the bridge from that installed version;
    # otherwise use this checkout for the first DCS start.
    bridge_source="$repo_dir/bridge"
    bridge_tmp="$(mktemp -d)"
    if docker inspect dcs-atc-bot >/dev/null 2>&1; then
      if ! docker cp dcs-atc-bot:/data/atc/src/bridge "$bridge_tmp/bridge" >/dev/null 2>&1; then
        echo "Could not read bridge files from the existing ATC bot container; refusing to deploy a possibly mismatched local copy." >&2
        rm -rf "$bridge_tmp"
        exit 1
      fi
      bridge_source="$bridge_tmp/bridge"
    fi
    deploy_bridge_from "$bridge_source"
    rm -rf "$bridge_tmp"
    compose up -d
    echo "Container running. Follow progress with: ./scripts/dcs.sh logs"
    ;;
  srs-autoconnect)
    deploy_srs_autoconnect
    ;;
  atc-up)
    require_docker
    # Refuse if a host bot is already using the map port (only one bot at a time).
    if ss -ltn 2>/dev/null | grep -q ':8090 '; then
      echo "Port 8090 is already in use (a host bot running?). Stop it first." >&2
      exit 1
    fi
    ref="${2:-$(latest_atc_tag)}"
    fetch_bridge_for_ref "$ref"
    ATC_REF="$ATC_RESOLVED_SHA" compose_atc up -d --build atc-bot
    cleanup_bridge_tmp
    echo "ATC bot starting at $ref ($ATC_RESOLVED_SHA). Follow it with: ./scripts/dcs.sh atc-logs"
    ;;
  atc-up-gpu)
    require_docker
    require_gpu
    if ss -ltn 2>/dev/null | grep -q ':8090 '; then
      echo "Port 8090 is already in use (a host bot running?). Stop it first." >&2
      exit 1
    fi
    # Needs the NVIDIA Container Toolkit on the host (see INSTALL.md).
    ref="${2:-$(latest_atc_tag)}"
    fetch_bridge_for_ref "$ref"
    ATC_REF="$ATC_RESOLVED_SHA" compose_atc_gpu up -d --build atc-bot
    cleanup_bridge_tmp
    echo "ATC bot (GPU) starting at $ref ($ATC_RESOLVED_SHA). Follow it with: ./scripts/dcs.sh atc-logs"
    ;;
  atc-up-stt-server)
    require_docker
    require_gpu
    if ss -ltn 2>/dev/null | grep -q ':8090 '; then
      echo "Port 8090 is already in use (a host bot running?). Stop it first." >&2
      exit 1
    fi
    # Bring up the standard services first so Compose creates its private
    # network. Speaches joins that network under the DNS name 'speaches'.
    compose_atc up -d srs-server
    stt_network="$(docker inspect --format '{{range $name, $config := .NetworkSettings.Networks}}{{println $name}}{{end}}' dcs-atc-srs | head -n 1)"
    if [[ -z "$stt_network" ]]; then
      echo "Could not determine the ATC Compose network for the STT server." >&2
      exit 1
    fi
    SPEACHES_DOCKER_NETWORK="$stt_network" "$repo_dir/scripts/stt-server.sh" start
    ref="${2:-$(latest_atc_tag)}"
    fetch_bridge_for_ref "$ref"
    ATC_REF="$ATC_RESOLVED_SHA" \
      ATC_STT_BACKEND=api \
      ATC_STT_BASE_URL=http://speaches:8000/v1 \
      ATC_STT_API_MODEL="${ATC_STT_API_MODEL:-Systran/faster-whisper-large-v3}" \
      ATC_STT_API_KEY= \
      compose_atc up -d --build atc-bot
    cleanup_bridge_tmp
    echo "ATC bot using Speaches STT starting at $ref ($ATC_RESOLVED_SHA). Follow it with: ./scripts/dcs.sh atc-logs"
    ;;
  stt-down)
    "$repo_dir/scripts/stt-server.sh" down
    ;;
  stt-status)
    "$repo_dir/scripts/stt-server.sh" status
    ;;
  atc-down)
    require_docker
    compose_atc stop atc-bot
    ;;
  atc-logs)
    require_docker
    compose_atc logs -f --tail=100 atc-bot
    ;;
  atc-update)
    require_docker
    ref="${2:-$(current_atc_sha)}"
    if [[ -z "$ref" ]]; then
      echo "Could not read the running bot version; pass a tag or SHA explicitly." >&2
      exit 1
    fi
    fetch_bridge_for_ref "$ref"
    # The entrypoint pulls on start, so a recreate is an update. Preserve GPU
    # mode if the running container was started with the GPU override.
    if atc_api_running; then
      # Preserve API mode on recreate. The endpoint and credentials are read
      # from the running container without displaying them.
      stt_base_url="$(atc_env_value ATC_STT_BASE_URL)"
      stt_model="$(atc_env_value ATC_STT_API_MODEL)"
      stt_api_key="$(atc_env_value ATC_STT_API_KEY)"
      stt_timeout="$(atc_env_value ATC_STT_API_TIMEOUT)"
      ATC_REF="$ATC_RESOLVED_SHA" ATC_STT_BACKEND=api \
        ATC_STT_BASE_URL="$stt_base_url" ATC_STT_API_MODEL="$stt_model" \
        ATC_STT_API_KEY="$stt_api_key" ATC_STT_API_TIMEOUT="$stt_timeout" \
        compose_atc up -d --force-recreate atc-bot
      cleanup_bridge_tmp
      echo "ATC bot (API STT) restarting at $ref ($ATC_RESOLVED_SHA)."
    elif atc_gpu_running; then
      require_gpu
      ATC_REF="$ATC_RESOLVED_SHA" compose_atc_gpu up -d --force-recreate atc-bot
      cleanup_bridge_tmp
      echo "ATC bot (GPU) restarting at $ref ($ATC_RESOLVED_SHA)."
    else
      ATC_REF="$ATC_RESOLVED_SHA" compose_atc up -d --force-recreate atc-bot
      cleanup_bridge_tmp
      echo "ATC bot restarting at $ref ($ATC_RESOLVED_SHA)."
    fi
    ;;
  bridge)
    deploy_state_bridge
    ;;
  stop)
    require_docker
    compose stop
    ;;
  status)
    require_docker
    compose ps
    ;;
  health)
    health
    ;;
  debriefs)
    require_docker
    docker exec dcs-atc-bot sh -c \
      'find /data/atc/src/atc/debrief -maxdepth 1 -type f \( -name "*.json" -o -name "debug_marks_*.jsonl" \) -printf "%f\n" 2>/dev/null | sort' \
      | sed 's#.*/##' || true
    ;;
  debrief)
    require_docker
    name="${2:-}"
    if [[ -z "$name" ]]; then
      echo "Usage: ./scripts/dcs.sh debrief <name|all>" >&2
      echo "List names with: ./scripts/dcs.sh debriefs" >&2
      exit 2
    fi
    dest="$repo_dir/atc/debrief"
    mkdir -p "$dest"
    if [[ "$name" == "all" ]]; then
      docker cp dcs-atc-bot:/data/atc/src/atc/debrief/. "$dest/"
      echo "Downloaded all debriefs to $dest"
    else
      docker cp "dcs-atc-bot:/data/atc/src/atc/debrief/$name" "$dest/$name"
      echo "Downloaded $name to $dest"
    fi
    ;;
  logs)
    require_docker
    compose logs -f --tail=100
    ;;
  srs-logs)
    require_docker
    compose logs -f --tail=100 srs-server
    ;;
  srs-status)
    require_docker
    curl -fsS http://127.0.0.1:8080/api/clients | python3 -m json.tool
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
