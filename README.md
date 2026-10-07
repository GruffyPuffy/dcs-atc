dcs-atc
=======

Standalone DCS ATC project: DCS dedicated server + SRS in Docker, plus a
headless Python ATC bot (SRS client, Whisper STT, Piper TTS, rules-based brain).

Layout
------
- `atc/` — the ATC bot (`atc_bot.py` full bot, `listen.py` listen-only, `debug_stt.py` offline STT tuning, `brain.py` rules brain, `srs_client.py` headless SRS client)
- `deploy/dcs/` — docker-compose for the DCS dedicated server and the SRS server
- `scripts/dcs.sh` — manage the containers (install/start/stop/logs/srs-*)
- `scripts/state_client.py` — CLI for the DCS state API (JSON socket bridge, port 10309): `status`, `move`, `move-geo`, `hold`
- `bridge/` — Saved Games hooks: `dcs_state_hook.lua` (state API), `srs_autoconnect.lua` (SRS announce)

Install
-------

### 1. Docker (once per host)

    sudo ./scripts/install-docker-ubuntu.sh

Installs Docker Engine + Compose plugin on Ubuntu 24.04. Log out/in (or
`newgrp docker`) so your user can run `docker` without sudo.

### 2. DCS + SRS containers

    ./scripts/dcs.sh install

First run: generates `deploy/dcs/.env` (Webtop user/password — see
`deploy/dcs/README.md` to set your own), creates `/data/dcs-atc/config`, pulls
the images and starts both containers. The DCS container then downloads and
installs the DCS dedicated server itself (several GB) — watch it with:

    ./scripts/dcs.sh logs

Wait until the log shows the DCS server running (Webtop at
`https://<lan-ip>:3001` also works for a first login check).

### 3. Install the Saved Games hooks

Once DCS is installed and running:

    ./scripts/dcs.sh bridge            # state-API hook (port 10309)
    ./scripts/dcs.sh srs-autoconnect   # optional: SRS announce on player join

Then restart the DCS process (Webtop → stop/start the server, or
`./scripts/dcs.sh stop && ./scripts/dcs.sh start`) so the hooks load. Verify
the state API:

    python3 scripts/state_client.py ping

### 4. Add a mission

Copy any `.miz` (e.g. TTI Caucasus) into the DCS Saved Games mission folder:

    /data/dcs-atc/config/.wine/drive_c/users/abc/Saved Games/DCS.dcs_serverrelease/Missions/

and select it in the DCS WebGUI. The mission needs at least one blue airbase
for the ATC bot to talk about; TTI Caucasus works as-is.

### 5. ATC bot

    cd atc
    uv sync                            # once; creates .venv from uv.lock
    ./start_bot.sh                     # joins SRS on 251.000 AM

First bot run downloads the Whisper model (`small.en`). The Piper voice is a
download too — copy `en_US-amy-medium.onnx(.json)` into `atc/voices/` or fetch
it with piper's downloader. Point the bot at a different frequency with
`./start_bot.sh --freq 124.0`.

Day-2 operation
---------------
- `./scripts/dcs.sh status` / `logs` / `stop` / `start` — container lifecycle
- `./scripts/dcs.sh srs-logs` / `srs-status` — SRS server log and connected clients
- `python3 scripts/state_client.py status` — live mission picture (groups,
  airbases, positions) used by the ATC logic

State API
---------
Read live DCS mission state (groups, airbases, positions) via the socket hook:

    python3 scripts/state_client.py status

The same `exchange()` helper is what the ATC logic will use to get the picture
(who is where, which runway/base is active) for real ATC decisions.
