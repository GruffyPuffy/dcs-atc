# Developing dcs-atc

This is the **developer** guide: run the bot on the host, edit code, run tests.
For getting the whole stack running as an operator, see
[`INSTALL.md`](INSTALL.md).

## The setup

DCS and SRS run in Docker (always on); the **bot runs on the host** for fast
iteration. The bot reaches them over localhost:

| From | To | Address |
| --- | --- | --- |
| host bot | DCS state API | `127.0.0.1:10309` |
| host bot | SRS | `127.0.0.1:5002` |

So first bring up the server side (once):

    ./scripts/dcs.sh install           # DCS + SRS containers
    ./scripts/dcs.sh bridge            # state-API hook
    ./scripts/dcs.sh srs-autoconnect   # SRS announce hook

Then run the bot on the host:

    cd atc
    uv sync                            # once; creates .venv from uv.lock
    ./start_bot.sh                     # joins SRS on 263.000 AM

First run downloads the Whisper model (`small.en`); Piper voices are fetched into
`atc/voices/` (gitignored). Useful flags:

    ./start_bot.sh --debug                 # richer logs + in-game comms mirror
    ./start_bot.sh --no-debrief            # skip the debrief file (saved by default)
    ./start_bot.sh --tx-volume 0.6         # ATC voice volume (default 0.6)
    ./start_bot.sh --airfield Gudauta      # a different airfield
    ATC_STT_MODEL=small.en ./start_bot.sh  # faster STT (default: medium.en)
    ./start_bot.sh --help                  # all options

A debrief (trail + comms) is saved for **every** session to
`atc/debrief/tracks_<field>_<time>.json` (the newest 20 are kept). A pilot can
also turn on an in-game comms mirror for themselves mid-flight by saying
**"Trainer, <callsign>, echo on"** on any frequency. A pilot can also set the
ATC voice volume mid-flight with **"Trainer, <callsign>, volume 0.3"** (0–1).

To restart the bot or switch airfield from the map page, use the supervisor:

    ./run_server.sh                        # map gets Restart / Stop buttons
    ./run_server.sh --debug                # + verbose DBG log + comms mirror
    ATC_AIRFIELD=Gudauta ./run_server.sh   # start on a specific airfield

## Tests

The brain is deterministic and fully offline (no DCS/SRS needed):

    cd atc
    uv run pytest                          # ~390 tests, <1s

Run it after any change to `brain`/`callsigns`/`phonetics`/`airspace`/`atis`/
`phraseology`/`workers`/`map_server`. See [`TESTING.md`](TESTING.md) for the
live test scenario.

## Switching between host and container

The bot can also run in its own container (the operator path). Only **one** bot
may run at a time — it owns SRS as ATC and the map port (8090).

    ./scripts/dcs.sh atc-down          # stop the container bot
    ./start_bot.sh                     # ...then run on the host

    # ...or the other way:
    # (Ctrl+C the host bot first)
    ./scripts/dcs.sh atc-up            # container bot (pulls latest main)

The container clones `main` from GitHub, so it only sees **committed** code —
commit before testing in the container. For a quick container test of your
working tree without committing, bind-mount it and skip the git update:

    docker run --rm --network host \
      -v "$PWD:/data/atc/src" -v atc-dev:/data/atc \
      -e PUID=$(id -u) -e PGID=$(id -g) -e ATC_SKIP_UPDATE=1 \
      -e ATC_SRS_HOST=127.0.0.1 -e ATC_STATE_HOST=127.0.0.1 \
      dcs-atc-bot:latest

## Layout

- `atc/` — the bot: `atc_bot.py` (wiring), `brain.py` (rules/state machine),
  `callsigns.py` + `phonetics.py` (STT tolerance), `airspace.py` + `ctr.py`
  (geometry), `atis.py`, `phraseology.json`, `map_server.py` + `web/` (map),
  `workers.py` (per-controller threads), `srs_client.py`, `speech.py`.
- `atc/tests/` — offline pytest suite (`scenario.py` flies full dialogs).
- `bridge/` — Saved Games hooks (state API, SRS announce).
- `scripts/` — `dcs.sh` (containers/hooks), `state_client.py`, `airfield.py`,
  `kneeboard.py`, `georef_tool.py`.
- `deploy/dcs/` — Compose files (`docker-compose.yml`, `docker-compose.atc.yml`).
- `ATC.md` — behaviour reference (keep in sync with `brain.py`).

## Conventions

- **Phraseology is real.** Only add calls that exist in the Master Arms SOP
  (wiki.masterarms.org) — no invented phraseology.
- **Never block the pilot.** A fumbled call must always have a way forward
  (`say again`, `help`, `reset`, `cancel`).
- **Keep `ATC.md` in sync** with behaviour changes, and add a test.
- **Deploy hooks after editing `bridge/`**: `./scripts/dcs.sh bridge` (the repo
  file is not auto-deployed; a hook change needs a DCS restart, a body-only
  change does not).
