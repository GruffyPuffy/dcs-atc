# Live test list

A casual checklist to fly through. Tick things off as you go; if something
looks wrong, note the **callsign + what you said + what you got** and we'll
check the log together.

- **Log:** `/tmp/atc_log.txt` (tail it in a second terminal: `tail -f /tmp/atc_log.txt`)
- **Audio:** `/tmp/atc_audio/` (`rx_*.wav` = what the bot heard, `tx_*.wav` = what it said)
- **Map:** http://localhost:8090/ (chatter drawer = best live debug)

---

## 0. Before you fly

- [ ] Offline suite is green: `cd atc && uv run pytest` → **221 passed**
- [ ] Start the bot: `./start_bot.sh --debug`
- [ ] You see the controller table + `[*] Ready.`
- [ ] Second terminal: `tail -f /tmp/atc_log.txt`
- [ ] Map opens at http://localhost:8090/

**Radios (AM):**

| Radio | Freq | Controller |
|-------|------|------------|
| 1 | 263.000 | Tower |
| 2 | 250.000 | Ground |
| 3 | 257.000 | Control |
| 4 | 270.500 | ATIS |

---

## 1. Startup / radio basics

- [ ] **ATC online** — on startup you hear *"Ground ATC online."* / *"Tower ATC
      online."* / *"Control ATC online."* (once per freq; `--no-announce` disables)
- [ ] **ATIS** (Radio 4) — British voice, weather + info letter + active runway
- [ ] **Radio check** — *"Colt 1, radio check"* → *"loud and clear"* (any freq)
- [ ] **Callsign** — say your flight name; bot only answers flights that exist

---

## 2. Ground (Radio 2, 250.000)

- [ ] **Check-in** — *"Ground, Colt 1, two-ship Hornets on Ramp South"*
      → *"Colt 1, Ground, runway 25 in use, QNH 2992."*
- [ ] **Check-in + ATIS** — *"…with information Oscar"* → just *"Colt 1, Ground."*
- [ ] **Clearance** — *"ready to copy clearance"* → exit point + altitude
- [ ] **Clearance readback** → *"readback correct"*
- [ ] **Taxi** — *"requesting taxi"* → route for **your ramp + active runway**
- [ ] **Taxi readback** → *"readback correct"*
- [ ] **Hold short** — *"holding short runway 25"* → names your holding point +
      *"contact Tower on channel 7"*

> Taxi routes to sanity-check (from the MA chart):
> Ramp South → 25 = **Sierra Echo**, → 07 = **Whiskey**;
> Ramp North → 25 = **November Delta**, → 07 = **November Alpha**;
> Ramp West → 25 = **November Delta**, → 07 = **Alpha**;
> Ramp East → 25 = **Echo**, → 07 = **Sierra**.

---

## 3. Tower — departure (Radio 1, 263.000)

- [ ] **Ready for departure** → *"line up and wait runway 25"*
- [ ] **Line-up readback** → *"readback correct, wind …, runway 25, right
      turnout, cleared for takeoff"*
- [ ] **Occupied runway** (optional) — park something on the runway, then ask
      → *"hold short, runway 25 is occupied"*

---

## 4. Control (Radio 3, 257.000)

- [ ] **Departure check-in** — *"Control, Colt 1, at 1500 ft"*
      → *"radar contact, climb to Angels 15"*
- [ ] **Inbound** — *"Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12"*
      → *"radar contact, turn right heading … to join via Entry …"*
- [ ] **Join readback** → *"descend to 1500 feet"*
- [ ] **Bearing & distance** — *"request bearing and distance to Entry East"*
      → *"bearing 014, distance 32 miles to Entry East"*
- [ ] **Vectors** — *"request vectors for runway 25"* → *"fly heading 018, vectors
      for runway 25"*

---

## 5. Tower — arrival (Radio 1, 263.000)

- [ ] **Entry** — *"Tower, Colt 1, Entry North"* → *"report runway in sight"*
- [ ] **Runway in sight** → *"cleared for left overhead break runway 25"*
- [ ] **On final** → *"runway 25, wind …, cleared to land"*
- [ ] **Occupied final** (optional) — traffic on the runway → *"continue
      approach, traffic on the runway"* / *"go around"*
- [ ] **Vacated** — *"runway vacated"* → *"contact Ground on channel 6"*
- [ ] **Taxi to parking** — *"requesting taxi to parking"* → named ramp + route

---

## 6. Trainer aids (any freq, any phase)

- [ ] **Help** — *"Colt 1, help"* → *"Apollo suggests: …"* hint for your phase
- [ ] **Say again** — *"Colt 1, say again"* → replays the last clearance
- [ ] **Cancel** — *"Colt 1, cancel"* → *"clearance cancelled"*, steps back one phase
- [ ] **Reset** — *"Colt 1, reset"* → back to Idle, *"contact Ground on channel 6"*
- [ ] **Say again fallback** — *"Tower, Colt 1, banana banana"* → *"say again"*

---

## 7. Map view (http://localhost:8090/)

- [ ] **Aircraft** — your aircraft shows as a coloured triangle + label
- [ ] **AI air** — AI aircraft appear (viewport-filtered)
- [ ] **Chart overlay** — toggle the georeferenced MA chart on/off
- [ ] **Check zones** — holding circles, final wedge, runway corridor
- [ ] **Traffic table** — live list of aircraft
- [ ] **Chatter drawer** — shows what was heard + replied; filter by agency
      (e.g. de-select ATIS); pilot lines show the SRS name + flight callsign
      (e.g. "Caveman (Colt 1)")

---

## 8. If something's off

- **Hears nothing** → SRS connected? radio on right freq + AM?
- **Always "say again"** → STT garbling; check the `"…"` line in the log
- **Wrong controller answers** → check the `[ground]`/`[tower]`/`[control]` tag
- **No live positions / no CTR warnings** → state bridge down; run
  `python3 scripts/state_client.py status`
- **Quiet audio** → check `peak … dBFS` in the log; try `--gain 3`

### Handy log greps

```bash
grep -E '\[(ground|tower|control)\]' /tmp/atc_log.txt   # what the bot heard
grep 'ATC (tx)' /tmp/atc_log.txt                        # what the bot said
grep 'DBG' /tmp/atc_log.txt                             # routing + pilot state
grep -E '<no matching intent>|<unclear>' /tmp/atc_log.txt  # not understood
grep -E 'CTR |RUNWAY OCCUPIED|ATIS ' /tmp/atc_log.txt   # events
```