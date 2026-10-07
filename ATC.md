# ATC Simulator — Behaviour Reference

This document describes what the ATC trainer understands, how it replies, and
how it tracks each pilot. It is the contract between the pilot (you, on SRS) and
the bot. Keep it in sync with `atc/brain.py` and `atc/airspace.json`.

The bot is **rules-based and deterministic** — no LLM. Speech is transcribed by
Whisper, matched against regexes that tolerate common STT errors, and answered
with canned phraseology. Live aircraft positions come from the DCS state bridge
(`bridge/dcs_state_hook.lua`), which lets replies be distance- and
traffic-aware.

---

## 1. Radio / channel

| Item | Value |
| --- | --- |
| Tower frequency | 263.000 MHz AM (Kutaisi) — configurable with `--freq` |
| ATIS frequency | 270.500 MHz AM (Kutaisi) — configurable with `--atis-freq` |
| Callsign | `Kutaisi Tower` (from `airspace.json`) |
| SRS | Bot joins as an External AWACS Mode client (password `atc`) |
| Coalition | Blue (2) |

Pilots call the tower; the tower answers. The bot only responds to transmissions
on its configured frequency. ATIS broadcasts on its own frequency (see §10).

---

## 2. Callsign recognition

The bot reads the **player-slot callsigns from the loaded mission** (`env.mission`
via the state bridge's `callsigns` op), so it only reacts to flights that actually
exist. In TTI Caucasus that is 14 flights: Anvil, Boar, Chevy, Colt, Dodge,
Enfield, Ford, Hawg, Heavy, Hornet, Pontiac, Springfield, Uzi, Viper.

STT mangles callsigns, so matching is tolerant:

- flight-name variants are **generated from phonetic confusion rules**
  (`atc/phonetics.py`), not hand-listed. Voiced/unvoiced pairs swap
  (`t`↔`d`, `p`↔`b`, `k`↔`g`), nasals blur (`m`↔`n`), vowels drift
  (`o`↔`u`, `a`↔`e`), and droppable letters (`l`, `r`, `t`, `d`, `h`) may vanish.
  So `Colt` yields `cold`, `bolt`, `coat`, `cult`, `kolt`, `cort`, … and
  `Ford` yields `fort`, `fold`, `vord`, … Variants that are themselves number
  words (e.g. `four` for `Ford`) are dropped to avoid false matches.
- flight numbers accept digits or spoken words, including homophones
  (`one`/`won`, `two`/`to`/`too`, `three`/`tree`, `four`/`for`, `eight`/`ate`)
- both `Colt 1` and `Colt 1-1` (flight + element) are recognised

Examples: `cold tree` → `Colt 3`, `kolt 2` → `Colt 2`, `fort 2` → `Ford 2`,
`hog 1` → `Hawg 1`, `vyper 1` → `Viper 1`, `springfeeld 2` → `Springfield 2`.

If no callsign is recognised, the bot stays silent (it assumes the transmission
was not for it). If a callsign is recognised but the request is not understood,
the tower replies **"say again"**.

> If the state bridge is unavailable, the bot falls back to a built-in list of
> common flight names. Tune the confusion rules in `atc/phonetics.py`
> (`CONFUSIONS`, `DROPPABLE`, `KNOWN_ERRORS`).

---

## 3. Phraseology the bot understands

| Pilot says (examples) | Intent | Tower replies |
| --- | --- | --- |
| "Kutaisi, Colt 1, requesting taxi to runway" | **taxi** | "Colt 1, Kutaisi Tower, taxi to runway 25 via alpha, hold short of runway 25." |
| "Colt 1, holding short" | **hold short** (after taxi) | "Colt 1, Kutaisi Tower, hold short runway 25." |
| "Colt 1, ready for departure" | **departure** (after taxi/holding) | "Colt 1, Kutaisi Tower, wind calm, runway 25, cleared for takeoff." |
| "Kutaisi, Colt 1, inbound" | **inbound** | distance-aware, see §4 |
| "Colt 1, checking in" / "with you" | **check-in** | "Colt 1, Kutaisi Tower, roger." |
| "Colt 1, roger" / "wilco" / "copy" | **readback** | "Colt 1, Kutaisi Tower, roger." |
| "Colt 1, help" | **help** | a short, state-aware hint (see §3a) |

Intent matching is order-sensitive: the first matching rule wins. Some rules
require a prior state (e.g. "ready for departure" only clears takeoff if the
aircraft has already been cleared to taxi).

---

## 3a. Help (trainer aid)

Because this is a *trainer*, a pilot who is unsure what to do can call
**"<callsign> help"** (also "assist", "what do I do", "what now", "remind me").
The bot replies with a short hint for the pilot's **current phase**, so it
always tells them the *next* call to make. It works on any controller
frequency.

Every hint is prefixed with a clear marker — **"Apollo suggests:"** — so it is
obvious the reply is training guidance, not a real clearance. (Apollo is the
Master Arms "Master of CTR" — a nod to the community whose SOP this trainer
follows.) The marker is the `help_prefix` variable in `phraseology.json`
(change it to taste, e.g. "ATC suggests" or "Training hint").

| Pilot phase | Help reply (after the "ATC suggests:" marker) |
| --- | --- |
| Idle (on the ground) | "contact Ground on channel 6 for clearance and taxi, then Tower on channel 7 for takeoff." |
| Cleared (clearance copied) | "read back your clearance, then request taxi." |
| Taxi | "taxi to runway 25 via alpha, then report holding short of runway 25." |
| Holding short | "you are holding short. Contact Tower on channel 7 and report ready for departure." |
| Departure | "you are cleared for takeoff runway 25. After departure turn right Exit East, 1500 ft or below, then contact Control on channel 8." |
| Airborne | "contact Control on channel 8 and report your position and intentions." |
| Inbound | "report entering the control zone, then report runway in sight." |
| Landing | "runway 25 is active. Report on final for landing clearance." |

The hint wording lives in `phraseology.json` (`help_*` templates); the
phase→template mapping is in `brain._help()`.

---

## 4. Distance-aware inbound handling

When the pilot calls inbound, the reply depends on the aircraft's live position
relative to the CTR (from the state bridge):

| Position | Tower replies |
| --- | --- |
| **Outside** the CTR | "Colt 1, Kutaisi Tower, roger, report entering the control zone, runway 25 active." |
| **Inside** the CTR | "Colt 1, Kutaisi Tower, radar contact 4 miles north, cleared control zone entry, join left downwind runway 25." |
| Position unknown (no state bridge) | "Colt 1, Kutaisi Tower, runway 25, wind calm, cleared to land." |

The position phrase uses distance + 8-point compass from the airfield
(e.g. "4 miles north").

---

## 5. Per-pilot state machine

Each callsign has an independent state, so the bot handles multiple aircraft in
multiplayer. States and transitions:

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Taxi: request taxi
    Taxi --> Holding: hold short
    Taxi --> Departure: ready for departure
    Holding --> Departure: ready for departure
    Departure --> Airborne: (leaves CTR)
    Airborne --> Inbound: calls inbound
    Inbound --> Landing: cleared to land
    Landing --> Idle: (on the ground)
```

| State | Meaning |
| --- | --- |
| `Idle` | On the ground, no clearance yet |
| `Taxi` | Cleared to taxi to the runway |
| `Holding` | Holding short of the runway |
| `Departure` | Cleared for takeoff |
| `Airborne` | Departed, outside the CTR |
| `Inbound` | Called inbound, cleared into the CTR |
| `Landing` | Cleared to land |

Each `PilotState` also tracks `cleared_inbound` (used to decide whether a CTR
entry is announced, see §6), `cleared_landing`, and `go_around_issued` (so a
go-around is only called once per approach, see §7).

---

## 6. CTR boundary monitoring

A background thread polls live positions every 2 seconds and tracks each
aircraft's inside/outside state (`atc/ctr.py`). On a boundary crossing:

| Event | Condition | Tower action |
| --- | --- | --- |
| `ENTERED_CTR` | Aircraft enters the CTR **without** having called inbound | Broadcast: "Colt 1, Kutaisi Tower, you are entering controlled airspace without clearance. Squawk 4201 and state intentions." |
| `ENTERED_CTR` | Aircraft had called inbound | No action (already cleared) |
| `EXITED_CTR` | Aircraft leaves the CTR | No action (currently) |

The CTR is defined in `airspace.json` as a polygon (Kutaisi) or a radius around
the airfield, with a ceiling in feet AGL. Kutaisi: surface to 1500 ft AGL.

---

## 7. Runway occupancy and go-around

The bot detects when the runway is occupied — by another player, an AI aircraft,
or ground traffic — and warns an aircraft on final.

| Condition | Tower action |
| --- | --- |
| Aircraft on final, runway occupied | "Colt 1, Kutaisi Tower, go around, runway 25 is occupied." |
| Aircraft on final, runway clear | (no call; the landing clearance stands) |

Detection:
1. **Runway occupancy** (`Airfield.runway_occupied`): any unit within a corridor
   around the runway centreline (default 1.6 NM long, ±0.12 NM wide) and below
   500 ft AGL. The aircraft being cleared is excluded so it does not count
   itself.
2. **Final approach** (`Airfield.is_on_final`): within 8 NM of the threshold,
   with both the bearing-to-threshold and the aircraft heading aligned with the
   runway within 30°.
3. The go-around fires **once per approach** (reset when the aircraft is no
   longer on final).

Runway heading is computed from the actual threshold-to-threshold bearing when
both runway ends are defined in `airspace.json` (accurate), falling back to the
runway number otherwise.

> Status: **implemented** (`brain.check_final`, `airspace.runway_occupied`,
> `airspace.is_on_final`).

---

## 8. What the bot does NOT do (yet)

- No squawk/transponder handling (the "squawk 4201" line is canned).
- No sequencing of multiple arrivals (first-come, first-served).
- No IFR clearances, holds, or instrument approaches.
- No taxi route from live airfield data (route "via alpha" is canned).
- No departure handoff to Control (Tower does not yet say "contact Control").

---

## 9. Configuration

`atc/airspace.json` holds per-airfield data:

```json
{
  "airfields": {
    "Kutaisi": {
      "tower": "Kutaisi Tower",
      "frequency_mhz": 263.0,
      "active_runway": "25",
      "ctr": { "ceiling_ft_agl": 1500, "polygon": [[lat, lon], ...] },
      "runways": { "25": { "threshold": [lat, lon] } },
      "gates": { "East": [lat, lon], ... }
    }
  }
}
```

Kutaisi CTR geometry is derived from the Master Arms community wiki
(https://wiki.masterarms.se/index.php/Airport_Procedures).

`atc/phraseology.json` holds the **reply wording** as templates with
`{placeholders}` (`{callsign}`, `{tower}`, `{runway}`, `{wind}`, `{position}`,
`{taxi_route}`, `{downwind}`, `{squawk}`). Edit the wording here to match a
community's SOP without touching Python. The *logic* — which call triggers which
template, and the state machine — stays in `brain.py`.

```json
{
  "variables": { "wind": "calm", "taxi_route": "alpha", "downwind": "left" },
  "templates": {
    "taxi": "{callsign}, {tower}, taxi to runway {runway} via {taxi_route}, hold short of runway {runway}.",
    "takeoff": "{callsign}, {tower}, wind {wind}, runway {runway}, cleared for takeoff."
  }
}
```

> Design note: only the *words* are configurable, not the intent matching. Regex
> logic in JSON would be unreadable and untestable; keeping it in Python keeps
> the behaviour debuggable.

---

## 10. ATIS

The bot broadcasts ATIS on a **separate frequency** (Kutaisi: 270.500 MHz AM,
from `airspace.json`'s `atis_frequency_mhz`). It repeats every
`--atis-interval` seconds (default 60; `0` disables).

The broadcast is built from **live DCS weather** (the bridge's `weather` op) and
the airfield config:

- **Information letter** — NATO phonetic, derived from the current hour
  (`Alpha` at 00:00, `Charlie` at 02:00, …). Pilots say "with information
  Charlie" when contacting Ground.
- **Active runway** — chosen from the wind: the runway whose heading is most
  into wind (highest headwind component). Calm wind falls back to the configured
  active runway.
- **QNH** — DCS stores it in mmHg; converted to inches of mercury (760 → 29.92).
- **Weather** — CAVOK when visibility ≥ 10 km and cloud base ≥ 1500 m, else
  visibility and cloud base are read out.

Example broadcast:

> Kutaisi information Oscar. 25 in use. wind calm. QNH 29.92. CAVOK.
> Temperature 20. Advise on initial contact you have information Oscar.

Master Arms reference: ATIS is UHF channel 21 (270.x0, per-airfield); pilots
listen before contacting Ground and read back the active runway and QNH.

The **active runway and wind are shared with the tower**: the bot refreshes
weather every `--atis-interval` seconds and updates the brain, so taxi, takeoff
and landing clearances use the same runway the ATIS is advertising, and the
spoken wind matches the live weather (e.g. "wind 070 at 8").

## 11. Controllers (Ground / Tower / Control)

The bot answers on **three frequencies**, each mapped to a controller role. The
frequency a transmission arrives on selects the role, so the same callsign can
talk to Ground, then Tower, then Control without any manual switching.

| Controller | Frequency (Kutaisi) | Handles |
|------------|--------------------|---------|
| **Ground** | 250.000 MHz (`ground_frequency_mhz`) | Clearance delivery, taxi, hold-short |
| **Tower** | 263.000 MHz (`frequency_mhz`) | Line-up, takeoff, overhead break, landing |
| **Control** | 257.000 MHz (`control_frequency_mhz`) | CTR entry, inbound routing via entry point |

Frequencies and controller names come from `airspace.json`; CLI flags
`--ground-freq` / `--control-freq` override them (`0` disables a role).

### Ground

- "ready to copy clearance" → departure clearance with an **exit point**:
  *"after departure turn right East, 1500 ft or below"*.
- "requesting taxi" → taxi clearance to the active runway.
- "holding short runway 25" → handoff: *"contact Tower on channel 7"*.

### Tower

- "ready for departure" → takeoff clearance (wind + runway).
- "runway in sight" → overhead-break clearance.
- "inbound" / "on final" → distance-aware inbound reply (see §4).

### Control

- "inbound" / "checking in" → radar contact and routing to join via the
  **entry point** nearest the aircraft: *"turn right heading 150 to join via
  Entry East"*. The entry point is chosen from the aircraft's live position
  (`gate_locator`), falling back to a default.

### Handoffs

The bot issues the standard Master Arms handoffs as the flight progresses:

- Ground → Tower: *"contact Tower on channel 7"* (`contact_tower`).
- Tower → Control (departures): *"contact Control on channel 8"*
  (`contact_control`).

Entry/exit point names and the channel numbers live in `phraseology.json`
(`tower_channel`, `channel`) and `airspace.json` (`gates`).

### Worker threads

Each controller runs in its **own worker thread** with its own queue
(`atc/workers.py`). The SRS receive callback only enqueues a finished
transmission; the worker does the slow work (Whisper STT → brain → Piper TTS →
transmit). This means a slow transcription on Ground never blocks Tower.

All workers **share one `AtcBrain` and one `CtrTracker`**, guarded by a single
lock, so a pilot's state (phase, clearance, entry/exit gate) is consistent no
matter which controller they are talking to. Piper synthesis and the SRS
transmit queue are serialised with a separate lock.

### Voices

Each controller speaks with a **different Piper voice**, so Tower, Ground,
Control and ATIS sound like different people:

| Controller | Default voice | Flag |
|------------|---------------|------|
| Tower | `en_US-amy-medium` | `--voice` |
| Ground | `en_US-ryan-medium` | `--ground-voice` |
| Control | `en_US-lessac-medium` | `--control-voice` |
| ATIS | `en_GB-alan-medium` | `--atis-voice` |

Voices live in `atc/voices/` and are fetched with
`uv run python -m piper.download_voices --download-dir voices <name>`.
