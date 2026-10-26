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
  debriefs  List debrief files saved in the bot container
  debrief   Download a debrief (name, or 'all') to atc/debrief/ on the host
  logs      Follow recent container logs (Ctrl+C to exit)
srs-autoconnect  Install the SRS auto-connect announcer hook (port-only by default)
  config    Prepare configuration and validate Compose without starting
  srs-logs  Follow recent SRS server logs (Ctrl+C to exit)
  srs-status Show connected SRS clients (HTTP status API)
  atc-up [REF] Start the ATC bot (latest release tag by default; REF may be a tag or SHA)
  atc-up-gpu [REF] Start the ATC bot with GPU (needs nvidia-container-toolkit)
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
  deploy_hook "$repo_dir/bridge/dcs_state_hook.lua" "dcs_state_hook.lua"
  deploy_hook "$repo_dir/bridge/dcs_state_body.lua" "dcs_state_body.lua"
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
  local destination="$hooks/$filename"
  mkdir -p "$hooks"
  if [[ ! -w "$hooks" ]]; then
    echo "DCS Saved Games Hooks directory is not writable." >&2
    exit 1
  fi
  if [[ -f "$destination" ]] && cmp -s "$source" "$destination"; then
    echo "Already current: $filename"
  else
    install -m 644 "$source" "$destination"
    echo "Deployed: $destination"
  fi
  echo "Restart the DCS process to load a newly installed hook."
}

command="${1:-help}"
# Commands with positional arguments are limited to these explicit cases.
if [[ "$#" -gt 1 && "$command" != "debrief" \
      && "$command" != "atc-up" && "$command" != "atc-up-gpu" \
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
    ATC_REF="$ref" compose_atc up -d --build atc-bot
    echo "ATC bot starting at $ref. Follow it with: ./scripts/dcs.sh atc-logs"
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
    ATC_REF="$ref" compose_atc_gpu up -d --build atc-bot
    echo "ATC bot (GPU) starting at $ref. Follow it with: ./scripts/dcs.sh atc-logs"
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
    # The entrypoint pulls on start, so a recreate is an update. Preserve GPU
    # mode if the running container was started with the GPU override.
    if atc_gpu_running; then
      require_gpu
      ATC_REF="$ref" compose_atc_gpu up -d --force-recreate atc-bot
      echo "ATC bot (GPU) restarting at $ref."
    else
      ATC_REF="$ref" compose_atc up -d --force-recreate atc-bot
      echo "ATC bot restarting at $ref."
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
      'ls -1 /data/atc/src/atc/debrief/*.json 2>/dev/null' \
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
