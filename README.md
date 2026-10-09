dcs-atc
=======

Standalone DCS ATC project: DCS dedicated server + SRS in Docker, plus a
headless Python ATC bot (SRS client, Whisper STT, Piper TTS, rules-based brain).

Layout
------
- `atc/` — the ATC bot (`atc_bot.py` full bot, `listen.py` listen-only, `debug_stt.py` offline STT tuning, `brain.py` rules brain, `callsigns.py` mission callsign recognition, `phonetics.py` STT-variant generation, `atis.py` weather/ATIS, `srs_client.py` headless SRS client, `airspace.py` CTR geometry, `ctr.py` boundary tracker, `state_client.py` DCS state reader, `map_server.py` live map view, `airspace.json` per-airfield config, `phraseology.json` reply wording, `web/` map page)
- `deploy/dcs/` — docker-compose for the DCS dedicated server and the SRS server
- `scripts/dcs.sh` — manage the containers (install/start/stop/logs/srs-*)
- `scripts/state_client.py` — CLI for the DCS state API (JSON socket bridge, port 10309): `status`, `diag`, `eval`, `move`, `move-geo`, `hold`
- `scripts/kneeboard.py` — generate DCS kneeboard JPGs from the exact MA SOP dialogs (`uv run --with pillow kneeboard.py`)
- `kneeboard/` — generated phraseology cards (start/takeoff, RTB/landing, airborne)
- `bridge/` — Saved Games hooks: `dcs_state_hook.lua` (state API socket + mission-env
  injection), `dcs_state_body.lua` (mission-side state logic, read fresh per request),
  `srs_autoconnect.lua` (SRS announce)
- `ATC.md` — ATC behaviour reference (callsigns, phraseology, controllers, CTR)
- `TESTING.md` — first live test scenario + how to read the log
- `atc/tests/` — offline pytest suite (no DCS/SRS needed): `cd atc && uv run pytest`

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

Then restart the DCS process from Webtop (stop/start the server) so the hooks
load — no need to restart the container. Verify the state API:

    python3 scripts/state_client.py status

### 4. Add a mission

Copy any `.miz` (e.g. TTI Caucasus) into the DCS Saved Games mission folder:

    /data/dcs-atc/config/.wine/drive_c/users/abc/Saved Games/DCS.dcs_serverrelease/Missions/

and select it in the DCS WebGUI. The mission needs at least one blue airbase
for the ATC bot to talk about; TTI Caucasus works as-is.

### 5. ATC bot

    cd atc
    uv sync                            # once; creates .venv from uv.lock
    ./start_bot.sh                     # joins SRS on 263.000 AM

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

How it works (no mission edits required)
----------------------------------------
DCS runs several isolated Lua VMs. The Simulator Scripting Engine API
(`coalition`, `Group`, `coord`) only works in the **mission** state, and the
Saved Games hook runs in the **gui** state (whose `coalition` is a stub). The
hook therefore injects the mission-side logic into the mission state at runtime:

    hook (gui) --net.dostring_in('mission')--> a_do_script(body) --> SSE API

`bridge/dcs_state_body.lua` holds that mission-side logic and is read fresh on
every request, so it can be edited and redeployed (`./scripts/dcs.sh bridge`)
without restarting DCS. Only changes to `dcs_state_hook.lua` itself need a DCS
restart. This works on any mission (e.g. Through The Inferno) with no `.miz`
changes and no `MissionScripting.lua` de-sanitize.

Debugging helpers:

    python3 scripts/state_client.py diag          # probe which mechanisms work
    python3 scripts/state_client.py eval '<expr>' # evaluate Lua in the hook env

Airspace / CTR
--------------
Control zones are defined in `atc/airspace.json` (per airfield: tower, frequency,
active runway, CTR polygon or radius, ceiling, runway thresholds, entry/exit
gates). The Kutaisi CTR geometry is derived from the Master Arms community wiki
(https://wiki.masterarms.se/index.php/Airport_Procedures): CTR surface to 1500 ft
AGL, TMA 1500 ft MSL to 10000 ft, CTA above.

`atc/airspace.py` builds the geometry with shapely (lat/lon projected to a local
metric plane) and answers containment/distance/bearing queries. `atc/ctr.py`
tracks per-aircraft inside/outside state and emits `ENTERED_CTR` / `EXITED_CTR`
events. The bot uses these to make inbound replies distance-aware and to warn on
unannounced CTR entry:

- inbound call outside the CTR → "report entering the control zone"
- inbound call inside the CTR → "radar contact <position>, cleared control zone
  entry, join left downwind runway 25"
- unannounced entry → "you are entering controlled airspace without clearance..."

The bot reads live positions from the state bridge (`--state-host/--state-port`,
disable with `--no-state`). It also detects an occupied runway and calls a
go-around for aircraft on final. See `ATC.md` for the full behaviour reference
(callsigns, phraseology, per-pilot state machine, CTR and runway logic).

Configuration is data-driven: `atc/airspace.json` is the single source of truth
for each airfield's tower name, frequency, ATIS frequency, active runway, CTR and
runways. Start the bot with `--airfield <name>` and everything else follows; CLI
flags (`--freq`, `--name`, `--atis-freq`) override if needed. Reply wording lives
in `atc/phraseology.json` (templates with `{placeholders}`), and callsigns are
read from the loaded mission's player slots (`env.mission`), so the bot only
reacts to flights that actually exist.

The bot also broadcasts **ATIS** on its own frequency (Kutaisi 270.500 AM),
built from live DCS weather: active runway from wind, QNH, CAVOK/visibility, and
an hour-based information letter (Alpha, Bravo, …). See `ATC.md` §10.

Map view
--------

The bot can serve a **live map** (Leaflet + OpenStreetMap) showing the CTR,
gates, runways, taxi routes and parking, plus every player aircraft with its
callsign, flight phase and current controller. `start_bot.sh` enables it by
default on port 8090; to run the bot directly, pass `--map-port`:

    ./atc/start_bot.sh                      # map on http://<host>:8090/
    uv run atc_bot.py --airfield Kutaisi --map-port 8090

Then open `http://<host>:8090/`. It can also run standalone (no bot) with
`uv run map_server.py --airfield Kutaisi --port 8090`. No extra Python
dependencies — the server is stdlib `http.server`; Leaflet loads from a CDN.
If the port is already in use the bot logs a warning and keeps running without
the map. The page also has a collapsible **Chatter** drawer (recent radio
traffic, filterable by agency) for live debugging.

License
-------

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Stefan Grufman ("Caveman").
