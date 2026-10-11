#!/usr/bin/env bash
# Install the NVIDIA Container Toolkit so Docker containers can use the GPU.
# Ubuntu/Debian. Run with sudo. After this, restart Docker and use
# `./scripts/dcs.sh atc-up-gpu`.
#
# Based on NVIDIA's official instructions:
#   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi not found — install the NVIDIA driver first." >&2
  exit 1
fi

echo "[*] Adding the NVIDIA Container Toolkit apt repository..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo "[*] Installing nvidia-container-toolkit..."
apt-get update
apt-get install -y nvidia-container-toolkit

echo "[*] Configuring the Docker runtime..."
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

echo "[*] Done. Verify with:"
echo "    docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi"
echo "Then start the bot with: ./scripts/dcs.sh atc-up-gpu"
