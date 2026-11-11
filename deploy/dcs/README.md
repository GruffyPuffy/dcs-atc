# DCS server setup and operation

The [upstream Aterfax image](https://github.com/Aterfax/DCS-World-Dedicated-Server-Docker) automates Wine and the dedicated server installation. This project supplies a small Compose wrapper for our `/data` layout. The container has been installed on this host and a Windows client has joined and flown an air-start slot.

## Why this layout

The container keeps its Wine prefix, DCS installation and saved games under `/config`, bound to `/data/dcs-atc/config` on the host. The current Saved Games mission directory is `/data/dcs-atc/config/.wine/drive_c/users/abc/Saved Games/DCS.dcs_serverrelease/Missions/`.

The Compose file publishes TCP and UDP 10308 for DCS clients and Webtop HTTPS port 3001 on the Ubuntu network interfaces. From Windows on the same trusted LAN, browse `https://<ubuntu-lan-ip>:3001`; accept the image's self-signed certificate warning. The Webtop username and password are stored in the local `.env`. The Eagle Dynamics web control port 8088 is not published for this trial. Do not forward port 3001 from the internet router: [Webtop's own security guidance](https://docs.linuxserver.io/images/docker-webtop/#security) recommends stronger access controls for internet exposure. A private VPN or authenticated reverse proxy can be added later if remote access is needed.

## SRS server (ATC trainer)

A second Compose service runs the [SimpleRadio Standalone server](https://github.com/ciribob/DCS-SimpleRadioStandalone) using the [`omltcat/dcs-srs-server`](https://github.com/omltcat/dcs-srs-docker) image — the official `SRS-Server-Commandline-Linux` binary, no Wine. SRS is independent of the DCS process: clients connect to it directly, so it does not need to run inside the DCS container. It exists for the planned ATC trainer; DCS players on the LAN can already use it as a normal SRS server.

Published ports: TCP+UDP 5002 (SRS voice/control, clients enter the Ubuntu LAN IP), and TCP 8080 on localhost only (SRS HTTP status API, used by the trainer to read connected clients). `CLIENT_EXPORT_ENABLED` also writes a clients JSON inside the container for debugging. External AWACS Mode is enabled with Blue password `atc` so clients can connect and transmit without DCS running — this is also how the headless Python ATC client will join later. Start it with `./scripts/dcs.sh start` (applies Compose changes without touching the DCS container's mission) and check `./scripts/dcs.sh logs`.

SRS voice never touches the DCS process: clients read their aircraft radios client-side and talk to the SRS server directly, so no mission or DCS-installation changes are needed. The optional `./scripts/dcs.sh srs-autoconnect` command installs a small Saved Games hook (`bridge/srs_autoconnect.lua`, deployed as `srs_autoconnect.lua`) that announces the SRS server in chat when a player joins; SRS clients with Auto Connect enabled then connect by themselves. By default it announces the **port only** (`SRS Running on 5002`), so the client reuses the host it used to reach DCS — this works for **LAN and internet** clients with no config. Set `SRS_HOST` to force an explicit address (e.g. a public hostname): `SRS_HOST=srs.example.com ./scripts/dcs.sh srs-autoconnect`. Restart the DCS process after installing it.

## Standalone STT server experiment

`./scripts/stt-server.sh up` starts the official Speaches CUDA container independently from the ATC bot. It uses the host NVIDIA GPU, caches downloaded models in a Docker volume, and publishes its API only on `127.0.0.1:18000`. Run `./scripts/stt-server.sh start` to start the container and ensure `Systran/faster-whisper-large-v3` is downloaded (override with `SPEACHES_MODEL=...`). For a local bot trial, `./atc/run_server.sh --stt-server` starts Speaches if needed and routes the bot to it. For the Docker bot, `./scripts/dcs.sh atc-up-stt-server` starts Speaches, attaches it to the private ATC Docker network, and starts the bot using API STT. The existing `./scripts/dcs.sh atc-up-gpu` command and local Whisper path are unchanged. Use `./scripts/stt-server.sh status`, `logs`, and `down`, or `./scripts/dcs.sh stt-status` / `stt-down`, to manage the server. `down` removes the container but keeps the model cache.

The API backend can also point at another OpenAI-compatible STT server. Set `ATC_STT_BACKEND=api`, `ATC_STT_BASE_URL` (ending in `/v1`), `ATC_STT_API_MODEL`, and optionally `ATC_STT_API_KEY`. For local `run_server.sh`, those variables are read directly from the shell. For the Docker bot, put them in `deploy/dcs/.env`; the regular bot command then uses the configured API backend.

This starts the STT service only; the bot does not yet send audio to it. Speaches loads models on demand. Check its logs and API docs at `http://127.0.0.1:18000/docs` before trying an audio request. The host port can be changed with `SPEACHES_HOST_PORT=...`. Keep the API bound to localhost during this experiment.

Docker's image and writable container layers are separate from the `/config` bind mount. On a fresh Docker Engine installation they use host system storage by default: usually `/var/lib/docker`, or `/var/lib/containerd` for image contents with Docker Engine 29's containerd image store. The [Docker Hub listing](https://hub.docker.com/r/aterfax/dcs-world-dedicated-server) currently reports roughly 1.8 GB for the image; extracted layers, updates and logs need more. The container's [installer script](https://raw.githubusercontent.com/Aterfax/DCS-World-Dedicated-Server-Docker/main/docker/src/wine-dedicated-dcs-automated-installer/dcs-dedicated-server-automatic-installer.sh) explicitly downloads the DCS updater in `/config` and installs DCS in its `/config`-backed Wine tree, so the large DCS installation and maps land on `/data`. Check `docker info --format '{{.DockerRootDir}}'`, `docker system df`, `df -h / /data` and `du -sh /data/dcs-atc/config` after installation. There is no need to relocate Docker's global storage for this first trial.

## Prepare

1. Confirm `/data` is mounted from the expected ext4 disk and is writable by your Linux user.
2. Install Docker Engine and Compose on Ubuntu 24.04. The included `scripts/install-docker-ubuntu.sh` follows [Docker's official apt repository instructions](https://docs.docker.com/engine/install/ubuntu/). Review it, then run `sudo ./scripts/install-docker-ubuntu.sh` from the project root. It installs system packages and needs internet access.
3. Run `./scripts/dcs.sh config`. It creates or updates the Git-ignored `deploy/dcs/.env`, fills in the current user's UID/GID and Webtop username, generates a Webtop password if needed, creates `/data/dcs-atc/config`, and validates Compose. To view the generated password locally, run `sed -n 's/^WEBTOP_PASSWORD=//p' deploy/dcs/.env`; do not paste it into logs or chat.
4. Run `./scripts/dcs.sh install` to download the image and start the container. Watch `./scripts/dcs.sh logs` and use Webtop for the first DCS login. Auto installation and DCS server auto start are on. The DCS launcher must have saved login and auto login enabled for server auto start to work.

The manager is safe to rerun after Compose changes. `config`, `install` and `start` refresh UID/GID and preserve any existing non-placeholder Webtop password and `/data/dcs-atc/config` contents. `start` and `install` use `docker compose up -d`, which applies configuration changes without deleting the persistent bind mount; they do not intentionally force a DCS reinstall (`FORCEREINSTALL=0`). Avoid applying Compose changes in the middle of a DCS updater run because Docker may recreate the container.

## Management commands

Run these from the project root:

| Command | Action |
| --- | --- |
| `./scripts/dcs.sh config` | Prepare and validate configuration without starting |
| `./scripts/dcs.sh install` | First container start; image pulls if missing |
| `./scripts/dcs.sh start` | Start or apply Compose changes |
| `./scripts/dcs.sh stop` | Stop without deleting persistent data |
| `./scripts/dcs.sh status` | Show container status |
| `./scripts/dcs.sh logs` | Follow recent logs; Ctrl+C exits the log view |
| `./scripts/dcs.sh srs-logs` | Follow recent SRS server logs; Ctrl+C exits the log view |
| `./scripts/dcs.sh srs-status` | Show connected SRS clients via the HTTP status API |
| `./scripts/dcs.sh srs-autoconnect` | Install the SRS auto-connect announcer hook (port-only by default; `SRS_HOST=...` to force a host) |


The setup script expects the current user to have Docker access. If `docker compose` reports a daemon permission error, the documented [Docker post-install options](https://docs.docker.com/engine/install/linux-postinstall/) include running Docker commands with sudo or adding the user to the `docker` group. Membership in that group grants root-level control over the host, so choose it deliberately.
