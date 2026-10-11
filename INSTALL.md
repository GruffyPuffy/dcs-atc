# Installing dcs-atc

Setup for a DCS dedicated server with the SRS server and the ATC bot, on a Linux
host (Ubuntu 24.04 tested). For the first live test after this, see
[`TESTING.md`](TESTING.md); for ATC behaviour, see [`ATC.md`](ATC.md).

This is the **operator** guide: get the whole stack running. If you want to
**develop** the bot (run it on the host, edit code, run tests), see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## What you get

Three containers, one per service, in a single Compose project (`dcs-atc`) on a
shared network:

| Container | Service | Image | Ports |
| --- | --- | --- | --- |
| `dcs-atc-server` | DCS dedicated server | `aterfax/dcs-world-dedicated-server` | 10308 tcp/udp, 3001 (Webtop, LAN), 10309 (localhost) |
| `dcs-atc-srs` | SRS server | `omltcat/dcs-srs-server` | 5002 tcp/udp, 8080 (localhost) |
| `dcs-atc-bot` | ATC bot | built from `atc/Dockerfile` | 8090 (map, LAN/VPN) |

DCS and SRS are always-on (started by `./scripts/dcs.sh install`). The ATC bot
is optional and started separately (`./scripts/dcs.sh atc-up`) — so you can run
it in its container **or** on the host. They reach each other by service name
(`dcs-server:10309`, `srs-server:5002`).

## 1. Docker (once per host)

    sudo ./scripts/install-docker-ubuntu.sh

Installs Docker Engine + Compose plugin on Ubuntu 24.04. Log out/in (or
`newgrp docker`) so your user can run `docker` without sudo.

## 2. DCS + SRS containers

    ./scripts/dcs.sh install

First run: generates `deploy/dcs/.env` (Webtop user/password — see
`deploy/dcs/README.md` to set your own), creates `/data/dcs-atc/config`, pulls
the images and starts both containers. The DCS container then downloads and
installs the DCS dedicated server itself (several GB) — watch it with:

    ./scripts/dcs.sh logs

Wait until the log shows the DCS server running (Webtop at
`https://<lan-ip>:3001` also works for a first login check).

## 3. Install the Saved Games hooks

Once DCS is installed and running:

    ./scripts/dcs.sh bridge            # state-API hook (port 10309)
    ./scripts/dcs.sh srs-autoconnect   # optional: SRS announce on player join

Then restart the DCS process from Webtop (stop/start the server) so the hooks
load — no need to restart the container. Verify the state API:

    python3 scripts/state_client.py status

## 4. Add a mission

Copy any `.miz` (e.g. TTI Caucasus) into the DCS Saved Games mission folder:

    /data/dcs-atc/config/.wine/drive_c/users/abc/Saved Games/DCS.dcs_serverrelease/Missions/

and select it in the DCS WebGUI. The mission needs at least one blue airbase
for the ATC bot to talk about; TTI Caucasus works as-is.

## 5. ATC bot

    ./scripts/dcs.sh atc-up            # build + start the bot container

The container is a thin runtime: on start it clones/updates the repo into a
persistent volume, syncs the Python env, downloads the Whisper model + Piper
voices, then runs the bot. So a restart always runs the latest `main`, and
nothing is baked into the image. It reaches DCS and SRS by their compose service
names, and serves the map on `http://<host>:8090/` (LAN/VPN only).

    ./scripts/dcs.sh atc-logs          # follow the bot log
    ./scripts/dcs.sh atc-update        # recreate (pulls latest on start)
    ./scripts/dcs.sh atc-down          # stop the bot container
    ./scripts/dcs.sh health            # read-only health summary
    ./scripts/dcs.sh debriefs          # list saved debriefs in the container
    ./scripts/dcs.sh debrief all       # download them to atc/debrief/

Only one bot may run at a time (it owns SRS as ATC and the map port). Stop the
host bot before `atc-up`, and vice versa.

**Speech-to-text.** The bot transcribes pilot calls with Whisper. By default it
**auto-selects**: `large-v3` on a GPU, `medium.en` on CPU. On CPU,
`medium.en` is accurate enough to hear callsigns reliably (~3 s per call on a
modern CPU); on a **low-power host** (few cores / older CPU) switch to the
faster, less accurate `small.en`:

    ATC_STT_MODEL=small.en ./scripts/dcs.sh atc-up

(Set it in `deploy/dcs/.env` to make it stick.) Rough guide: `medium.en` wants
~4 CPU cores; `small.en` runs on ~2.

### GPU (optional, much faster + more accurate)

With an NVIDIA GPU the bot runs `large-v3` — better accuracy *and*
sub-second latency. This needs the **NVIDIA Container Toolkit** on the host:

    # once per host (Ubuntu):
    sudo ./scripts/install-nvidia-container.sh   # or follow NVIDIA's docs
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker

Then start the bot with the GPU override:

    ./scripts/dcs.sh atc-up-gpu

This is the **same** `atc-bot` container, recreated in place with the GPU
reservation — not a second container. Your venv, Whisper model and voices in the
`atc-data` volume are kept. Switch back to CPU any time with `./scripts/dcs.sh
atc-up` (recreates it without the override). `atc-update` remembers the mode.

The container installs the CUDA runtime libs (cuBLAS/cuDNN) and uses the GPU.
`atc-up-gpu` checks for the driver and the Docker NVIDIA runtime first, and tells
you to run the installer above if either is missing. Without a GPU, `atc-up`
(CPU) is unchanged. Force a device/model with `ATC_STT_DEVICE` / `ATC_STT_MODEL`
if needed.

With the bot running, tune your radio to the tower frequency and fly the MA
flow: request clearance and taxi on Ground, line up and take off on Tower, then
check in with Control. See the generated kneeboard cards (`kneeboard/`) for the
exact calls at each step. Then review a sortie afterwards: open the map and pick
your debrief from the **debrief** dropdown (see the *Map view* section of the
[README](README.md)).

The map's **Restart** button writes the chosen airfield to `atc/.control` and
the bot exits with code 75; `run_server.sh` (the container entrypoint) relaunches
it on the new airfield. Only code 75 loops (Ctrl+C / crash / **Stop** end it).
Keep the map on your LAN or VPN — never forward it to the internet.

> **Developing the bot?** Run it on the host instead (faster iteration, live
> code, tests) — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Day-2 operation

- `./scripts/dcs.sh status` / `logs` / `stop` / `start` — container lifecycle
- `./scripts/dcs.sh health` — read-only summary (containers, bot mode, map, bridge)
- `./scripts/dcs.sh srs-logs` / `srs-status` — SRS server log and connected clients
- `./scripts/dcs.sh atc-up` / `atc-down` / `atc-logs` / `atc-update` — ATC bot
- `./scripts/dcs.sh debriefs` / `debrief <name|all>` — list/download debriefs
- `python3 scripts/state_client.py status` — live mission picture (groups,
  airbases, positions) used by the ATC logic
