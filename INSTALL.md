# Installing dcs-atc

Setup for a DCS dedicated server with the SRS server and the ATC bot, on a Linux
host (Ubuntu 24.04 tested). For the first live test after this, see
[`TESTING.md`](TESTING.md); for ATC behaviour, see [`ATC.md`](ATC.md).

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

    cd atc
    uv sync                            # once; creates .venv from uv.lock
    ./start_bot.sh                     # joins SRS on 263.000 AM

First bot run downloads the Whisper model (`small.en`). The Piper voice is a
download too — copy `en_US-amy-medium.onnx(.json)` into `atc/voices/` or fetch
it with piper's downloader. Point the bot at a different frequency with
`./start_bot.sh --freq 124.0`.

With the bot running, tune your radio to the tower frequency and fly the MA
flow: request clearance and taxi on Ground, line up and take off on Tower, then
check in with Control. See the generated kneeboard cards (`kneeboard/`) for the
exact calls at each step. Useful extras:

    ./start_bot.sh --debug                 # + richer logs + a saved debrief file
    ./start_bot.sh --help                  # all bot options

Then review a sortie afterwards: open the map and pick your debrief from the
**debrief** dropdown (see the *Map view* section of the [README](README.md)).

## Day-2 operation

- `./scripts/dcs.sh status` / `logs` / `stop` / `start` — container lifecycle
- `./scripts/dcs.sh srs-logs` / `srs-status` — SRS server log and connected clients
- `python3 scripts/state_client.py status` — live mission picture (groups,
  airbases, positions) used by the ATC logic
