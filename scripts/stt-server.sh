#!/usr/bin/env bash
set -euo pipefail

# Standalone Speaches GPU server for experimenting with remote STT. This is
# deliberately separate from the ATC bot and from scripts/dcs.sh.
container_name="dcs-atc-speaches"
image="${SPEACHES_IMAGE:-ghcr.io/speaches-ai/speaches:latest-cuda-12.6.3}"
host_port="${SPEACHES_HOST_PORT:-18000}"
cache_volume="dcs-atc-speaches-cache"
model="${SPEACHES_MODEL:-Systran/faster-whisper-large-v3}"
docker_network="${SPEACHES_DOCKER_NETWORK:-}"

usage() {
  cat <<'EOF'
Usage: ./scripts/stt-server.sh <up|start|download-model|down|status|logs>

Starts Speaches as an isolated GPU STT server. It is only published on
127.0.0.1:18000 by default. Override with SPEACHES_HOST_PORT if needed.

Commands:
  up      Pull the configured image (if needed) and start the container
  start   Start it, wait for the API, and ensure the configured model is cached
  download-model  Download the default large-v3 model into Speaches' cache
  down    Stop and remove the container (keeps the model cache volume)
  status  Show container state and local API reachability
  logs    Follow Speaches logs

Optional environment:
  SPEACHES_IMAGE       Image reference (default: official CUDA 12.6.3 image)
  SPEACHES_HOST_PORT   Host port (default: 18000)
  SPEACHES_MODEL       Model to download (default: Systran/faster-whisper-large-v3)
EOF
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "Docker Engine is required and must be accessible to this user." >&2
    exit 1
  fi
}

connect_to_requested_network() {
  [[ -n "$docker_network" ]] || return 0
  if ! docker network inspect "$docker_network" >/dev/null 2>&1; then
    echo "Docker network '$docker_network' does not exist." >&2
    exit 1
  fi
  local networks
  networks="$(docker inspect --format '{{range $name, $config := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$container_name")"
  if ! grep -Fxq "$docker_network" <<<"$networks"; then
    docker network connect --alias speaches "$docker_network" "$container_name"
  fi
}

ensure_api_and_model() {
  local attempt models
  for attempt in {1..60}; do
    if curl --fail --silent "http://127.0.0.1:${host_port}/v1/models" >/dev/null; then
      break
    fi
    if [[ "$attempt" == "60" ]]; then
      echo "Speaches API did not become ready; inspect logs with ./scripts/stt-server.sh logs" >&2
      exit 1
    fi
    sleep 2
  done
  models="$(curl --fail --silent --show-error "http://127.0.0.1:${host_port}/v1/models")"
  if python3 -c 'import json,sys; sys.exit(0 if any(m.get("id")==sys.argv[1] for m in json.load(sys.stdin).get("data", [])) else 1)' "$model" <<<"$models"; then
    echo "STT model ready: $model"
  else
    curl --fail --silent --show-error --request POST \
      "http://127.0.0.1:${host_port}/v1/models/${model}"
    printf '\nSTT model downloaded: %s\n' "$model"
  fi
}

case "${1:-help}" in
  up)
    require_docker
    if ! command -v nvidia-smi >/dev/null 2>&1; then
      echo "No NVIDIA driver found (nvidia-smi missing)." >&2
      exit 1
    fi
    if docker container inspect "$container_name" >/dev/null 2>&1; then
      docker start "$container_name" >/dev/null
      connect_to_requested_network
    else
      docker pull "$image"
      docker volume create "$cache_volume" >/dev/null
      network_args=()
      if [[ -n "$docker_network" ]]; then
        docker network inspect "$docker_network" >/dev/null 2>&1 || {
          echo "Docker network '$docker_network' does not exist." >&2
          exit 1
        }
        network_args=(--network "$docker_network" --network-alias speaches)
      fi
      docker run --detach \
        --name "$container_name" \
        --restart unless-stopped \
        --gpus all \
        "${network_args[@]}" \
        --publish "127.0.0.1:${host_port}:8000" \
        --volume "$cache_volume:/home/ubuntu/.cache/huggingface/hub" \
        "$image" >/dev/null
    fi
    printf 'Speaches started: http://127.0.0.1:%s\n' "$host_port"
    printf 'API docs:         http://127.0.0.1:%s/docs\n' "$host_port"
    printf 'Follow startup:   ./scripts/stt-server.sh logs\n'
    ;;
  start)
    "$0" up
    ensure_api_and_model
    ;;
  down)
    require_docker
    if docker container inspect "$container_name" >/dev/null 2>&1; then
      docker rm --force "$container_name" >/dev/null
      echo "Speaches container removed; the cached model volume was kept."
    else
      echo "Speaches container is not present."
    fi
    ;;
  download-model)
    require_docker
    if ! docker container inspect "$container_name" >/dev/null 2>&1; then
      echo "Start Speaches first with ./scripts/stt-server.sh up" >&2
      exit 1
    fi
    ensure_api_and_model
    ;;
  status)
    require_docker
    if docker container inspect "$container_name" >/dev/null 2>&1; then
      docker inspect --format 'container={{.State.Status}} image={{.Config.Image}}' "$container_name"
      if curl --fail --silent --show-error "http://127.0.0.1:${host_port}/v1/models" >/dev/null; then
        echo "API reachable at http://127.0.0.1:${host_port}"
      else
        echo "API is not responding yet; inspect logs with ./scripts/stt-server.sh logs" >&2
        exit 1
      fi
    else
      echo "Speaches container is not present. Start it with ./scripts/stt-server.sh up"
      exit 1
    fi
    ;;
  logs)
    require_docker
    docker logs --follow "$container_name"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
