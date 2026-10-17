# Thin runtime for the ATC bot. The application code is NOT baked in: the
# entrypoint clones/updates the repo into a persistent volume on start, so a
# restart always runs the latest `main` (like the DCS image's auto-installer).
FROM python:3.12-slim

# git: clone/update the repo. libgomp1: OpenMP runtime for ctranslate2
# (faster-whisper). ca-certificates: HTTPS for git + model/voice downloads.
# gosu: drop from root to the host UID/GID after fixing volume ownership.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates libgomp1 gosu openssl \
    && rm -rf /var/lib/apt/lists/*

# uv (fast, reproducible installs from uv.lock).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Everything persistent lives under /data/atc (a mounted volume): the checkout,
# the venv, the Whisper model cache and the Piper voices. So restarts are fast
# and an update is just a `git pull`.
ENV ATC_DIR=/data/atc/src \
    UV_PROJECT_ENVIRONMENT=/data/atc/.venv \
    UV_CACHE_DIR=/data/atc/uv-cache \
    UV_LINK_MODE=copy \
    HF_HOME=/data/atc/hf \
    PYTHONUNBUFFERED=1

COPY docker/entrypoint.sh /usr/local/bin/atc-entrypoint
RUN chmod +x /usr/local/bin/atc-entrypoint

WORKDIR /data/atc
ENTRYPOINT ["/usr/local/bin/atc-entrypoint"]
