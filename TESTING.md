# First live test — step-by-step

A simple, scripted first test to run once you are logged into DCS. Follow the
steps in order; each one exercises a different part of the bot. Afterwards we
read the log together to see what worked.

Everything is logged to **`/tmp/atc_log.txt`** (and printed to the terminal).
Captured audio is saved to `/tmp/atc_audio/` (`rx_*.wav` = what the bot heard,
`tx_*.wav` = what the bot said).

---

## 0a. Offline tests (no DCS, no SRS)

Before touching the server, run the offline suite — it exercises the pure logic
(callsign recognition, phraseology, brain state machine, airspace geometry,
ATIS, controller routing, worker concurrency) with no server or SRS:

    cd ~/projects/dcs-atc/atc
    uv run pytest

Expected: `56 passed`. This catches most regressions in seconds. Run it after
any change to `brain.py`, `callsigns.py`, `phonetics.py`, `airspace.py`,
`atis.py`, `phraseology.json` or `workers.py`.

You can also drive the brain by hand without any audio:

    uv run python -c "
    from airspace import Airspace
    from brain import AtcBrain, Controller, Phraseology
    from callsigns import CallsignRegistry
    af = Airspace.load().get('Kutaisi')
    cs = CallsignRegistry.from_mission([{'name':'Colt 1'}])
    b = AtcBrain(tower=af.tower, runway=af.active_runway,
                 phraseology=Phraseology.load(), callsigns=cs,
                 ground=af.ground, control=af.control,
                 gates=list(af.gates), gate_locator=af.nearest_gate)
    print(b.handle('Ground, Colt 1, requesting taxi', controller=Controller.GROUND))
    "

---

## 0b. Before you fly (on the ground, in a terminal)

Start the bot with debug logging:

    cd ~/projects/dcs-atc/atc
    ./start_bot.sh --debug

You should see it load Whisper + the four voices, then print the controller
table and `[*] Ready.`:

    [*] Airfield Kutaisi: Kutaisi Tower, 263.000 MHz, runway 25, CTR ceiling 1500 ft AGL
    [*] Ready.
    [HH:MM:SS] ATC bot listening on 263.000 MHz AM @ 127.0.0.1:5002 (log: /tmp/atc_log.txt)
    [HH:MM:SS]   ground   250.000 MHz  voice en_US-ryan-medium
    [HH:MM:SS]   tower    263.000 MHz  voice en_US-amy-medium
    [HH:MM:SS]   control  257.000 MHz  voice en_US-lessac-medium
    [HH:MM:SS] ATIS broadcasting on 270.500 MHz AM every 60s  voice en_GB-alan-medium

Leave this terminal running. Open a **second terminal** for the log tail:

    tail -f /tmp/atc_log.txt

---

## 1. In DCS — set up your radios

Spawn a **blue** aircraft at **Kutaisi** (e.g. a Hornet on the ramp). Set your
radios to the bot's frequencies (AM):

| Radio | Frequency | Controller |
|-------|-----------|------------|
| Radio 1 | **263.000** | Tower |
| Radio 2 | **250.000** | Ground |
| Radio 3 | **257.000** | Control |
| Radio 4 | **270.500** | ATIS |

> Tip: use the SRS overlay to confirm you are transmitting on the right radio.

---

## 2. The radio calls (do these in order)

Say each line clearly, then **wait for the reply** before the next one. The
callsign used below is **Colt 1** — use whatever flight you actually spawned as
(the bot learns callsigns from the mission).

### 2a. ATIS (listen only — no reply expected)

Tune Radio 4 to **270.500** and listen. You should hear a British voice reading
the weather, e.g. *"Kutaisi information Oscar. 25 in use. wind calm. QNH 29.92.
CAVOK…"*. Note the **information letter** and **active runway**.

### 2b. Ground — check-in, clearance + taxi (Radio 2, 250.000)

1. **"Ground, Colt 1, two-ship Hornets on Ramp South with information Oscar."**
   → expect: *"Colt 1, Ground."* (without "with information" you instead get
   *"Colt 1, Ground, runway 25 in use, QNH 2992."*)
2. **"Ground, Colt 1, ready to copy clearance."**
   → expect: *"Colt 1, Ground, after departure turn right Exit East, 1500 ft or below."*
3. **"After departure turn right Exit East, 1500 ft or below, Colt 1."**
   → expect: *"Colt 1, Ground, readback correct."*
4. **"Ground, Colt 1, requesting taxi."**
   → expect: *"Colt 1, Ground, cleared taxi Sierra Echo and hold short runway 25."*
5. **"Cleared taxi Sierra Echo and hold short runway 25, Colt 1."**
   → expect: *"Colt 1, Ground, readback correct."*
6. **"Colt 1, holding short runway 25."**
   → expect: *"Colt 1, Ground, contact Tower on channel 7."*

### 2c. Tower — line up + departure (Radio 1, 263.000)

7. **"Tower, Colt 1, at runway 25, ready for departure."**
   → expect: *"Colt 1, Tower, line up and wait runway 25."*
8. **"Line up and wait 25, Colt 1."**
   → expect: *"Colt 1, Tower, readback correct, wind …, runway 25, right turnout, cleared for takeoff."*

### 2d. Control — departure check-in + inbound (Radio 3, 257.000)

9. **"Control, Colt 1, at 1500 ft."**
   → expect: *"Colt 1, Control, radar contact, climb to Angels 15."*
10. **"Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12."**
    → expect: *"Colt 1, Control, radar contact, turn right heading 150 to join via Entry North."*
11. **"150 to join via Entry North, Colt 1."**
    → expect: *"Colt 1, Control, descend to 1500 feet."*

### 2e. Tower — landing (Radio 1, 263.000)

12. **"Tower, Colt 1, Entry North."**
    → expect: *"Colt 1, Tower, report runway in sight."*
13. **"Tower, Colt 1, runway in sight."**
    → expect: *"Colt 1, Tower, wind …, cleared for left overhead break runway 25."*
14. **"Tower, Colt 1, on final."**
    → expect: *"Colt 1, Tower, runway 25, wind …, cleared to land."*
15. **"Tower, Colt 1, runway vacated."**
    → expect: *"Colt 1, Tower, contact Ground on channel 6."*

### 2f. Deliberate failure (optional)

16. **"Tower, Colt 1, banana banana."**
    → expect: *"Colt 1, Tower, say again."* (proves the fallback works)

### 2g. Help (trainer aid)

17. **"Colt 1, help."** (on any frequency)
    → expect a short hint for your current phase, prefixed with **"Apollo suggests:"**,
    e.g. on the ground: *"Colt 1, Apollo suggests: contact Ground on channel 6
    for clearance and taxi, then Tower on channel 7 for takeoff. Say reset to
    start over, or cancel to undo a clearance."*
    Repeat it after each step to see the hint change with your state.

### 2h. Escape hatches (never get stuck)

18. **"Colt 1, say again."**
    → expect the **last clearance** replayed verbatim.
19. **"Colt 1, cancel."**
    → expect *"Colt 1, <agency>, clearance cancelled."* and your phase to step
    back one (e.g. Line-up → Holding).
20. **"Colt 1, reset."**
    → expect *"Colt 1, <agency>, state reset. Contact Ground on channel 6 when
    ready."* and your phase to return to Idle.

---

## 3. After the flight

Stop the bot with **Ctrl+C** in its terminal. Then we look at the log:

    cat /tmp/atc_log.txt

### What to check in the log

For **each** call you made, look for a pair of lines:

    [HH:MM:SS] [ground] <your SRS name>: "Ground, Colt 1, requesting taxi"  (stt 320 ms, 2.1 s, peak -18 dBFS, wav rx_….wav)
    [HH:MM:SS] DBG [ground@250.000] <your SRS name> -> 'Colt 1, Ground, cleared taxi Sierra Echo and hold short runway 25.'  [phase=Taxi entry='' exit='']

- **`[ground]` / `[tower]` / `[control]`** — which frequency the call arrived on.
  If this is wrong, the radio was on the wrong frequency.
- **`"…"`** — what Whisper heard. If this is garbled, that is an STT problem
  (we can tune `phonetics.py` / the initial prompt).
- **`stt … ms`** — transcription latency. Should be a few hundred ms.
- **`peak … dBFS`** — how loud your transmission was. If it is very low
  (e.g. below -40), the bot may not hear you well — try `--gain 3`.
- **`DBG … -> '…'`** — the reply the brain produced, plus the pilot's phase and
  assigned entry/exit gate. If the reply is wrong, that is a brain/phraseology
  problem.
- **`<no matching intent>`** — the bot heard you but did not understand the
  request (regex miss).
- **`<unclear>`** — Whisper produced no text at all.

### Quick summary commands

    # every transmission the bot heard, with the controller tag
    grep -E '\[(ground|tower|control)\]' /tmp/atc_log.txt

    # every reply the bot sent
    grep 'ATC (tx)' /tmp/atc_log.txt

    # routing + pilot state (needs --debug)
    grep 'DBG' /tmp/atc_log.txt

    # anything the bot did not understand
    grep -E '<no matching intent>|<unclear>' /tmp/atc_log.txt

    # CTR / runway events
    grep -E 'CTR |RUNWAY OCCUPIED|ATIS ' /tmp/atc_log.txt

---

## 4. What we are testing (checklist)

| # | Feature | Expected |
|---|---------|----------|
| 1 | SRS connect + multi-frequency listen | bot hears you on 250/263/257 |
| 2 | ATIS broadcast | British voice, weather + info letter |
| 3 | Callsign recognition | "Colt 1" recognized from speech |
| 4 | Ground check-in | "Colt 1, Ground" (or runway + QNH without ATIS info) |
| 5 | Ground clearance + readback | exit point, then "readback correct" |
| 6 | Ground taxi + readback | taxi route + hold short, then "readback correct" |
| 7 | Ground→Tower handoff | "contact Tower on channel 7" |
| 8 | Tower line-up + readback | "line up and wait", then takeoff clearance |
| 9 | Control departure check-in | radar contact + climb to Angels |
| 10 | Control inbound + readback | radar contact + entry gate, then descend |
| 11 | Tower landing | report runway in sight → overhead break → cleared to land |
| 12 | Say-again fallback | unknown request → "say again" |
| 13 | Per-controller voices | Ground/Tower/Control sound different |
| 14 | Live state (if bridge up) | distance-aware replies, CTR warnings |
| 15 | Help (trainer aid) | "Colt 1 help" → state-aware hint |
| 16 | Escape hatches | reset → Idle, cancel → step back, say again → replay |

---

## 5. If something is wrong

- **Bot hears nothing at all** → check SRS is connected (bot log shows the
  client joining), and that your radio is on the right frequency + AM.
- **Bot hears you but always says "say again"** → STT is garbling the callsign
  or request; check the `"…"` line in the log.
- **Wrong controller answers** → the frequency mapping; check the `[ground]` /
  `[tower]` tag vs. the radio you used.
- **No live positions / no CTR warnings** → the state bridge is down; run
  `python3 scripts/state_client.py status` and check the bot log for
  `state bridge unavailable`.
