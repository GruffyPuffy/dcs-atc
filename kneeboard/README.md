# Kneeboard pages

Glanceable phraseology cards generated from the **exact** Master Arms SOP dialogs
(https://wiki.masterarms.se/index.php/Mission_Procedures), for use in the DCS
kneeboard during training. Page 5 is the Master Arms **comms board** (the UHF
channel plan from https://wiki.masterarms.se/index.php/Comms).

| Page | Covers |
| --- | --- |
| `01-start-takeoff.jpg` | Ground (Ch 6) + Tower (Ch 7): check-in, ATIS, clearance, taxi, line-up, takeoff, handoff to Control |
| `02-rtb-landing.jpg` | Control (Ch 8) + Tower (Ch 7) + Ground (Ch 6): inbound, join, descent, entry, break, landing, taxi to parking |
| `03-airborne.jpg` | AWACS / package (not flown in the sim): check-in, push, attack, RTB handoff |
| `04-light-path.jpg` | **Single-ship light path**: skip Ground (straight to Tower) and the overhead break (report "on final"); Tower → Control → land |
| `05-comms.jpg` | **Comms board**: the Master Arms UHF channel plan (Ch 1–25), with the channels this trainer uses highlighted |

Page 4 is a **shortened trainer path**, not a wiki transcript: it uses the ATC
bot's actual replies to show the fewest calls that get a single-ship airborne and
back. Stuck at any point? Say *"Trainer, <callsign>, help"* on any frequency.

Page 5 is the **comms board** — the Master Arms UHF channel plan, verbatim from
the wiki's Comms page. The channels this trainer uses (Ch 6 Ground, Ch 7 Tower,
Ch 8 Control, Ch 21 ATIS) are highlighted in orange.

Each other line is a request/answer exchange from the wiki (the "Adder11" two-ship
example), colour-coded by who is talking (PILOT / GND / TWR / CTL / AWACS).

**Radio convention shown on the cards** (tidied from the SOP for training):

- Identify yourself on **every** line — the callsign is the only "caller ID".
- **First call** on a frequency addresses the agency (*"Ground, Adder11"*);
  after that just the callsign (*"Adder11, requesting taxi"*).
- On a **readback**, say the content first and the callsign **last**
  (*"Cleared taxi Sierra Echo and hold short Runway 25, Adder11"*).
- **Controllers** address the pilot on every line (*"Adder11, …"*) and name
  their own facility only on first contact.
- **On every radio switch**: acknowledge the handoff, then **check in as a
  flight** on the new frequency (*"Adder1"*, then each wingman answers *"2"*,
  *"3"*, *"4"* …), then make the first call with the agency prefix (*"Tower,
  Adder11, Entry East"*). The flight check-in is a flight-internal call — the
  controller does not answer it.

## Deliberate deviations from the wiki

The cards are the wiki dialogs with a few **intentional** corrections, so the
trainee learns the correct form rather than the wiki's slips:

| Wiki | Card | Why |
| --- | --- | --- |
| QNH readback: *"25 in use, QNH 2992."* (no callsign) | *"25 in use, QNH 2992, Adder11."* | every line must identify the sender |
| *"4-ship Adder11"* on the break/landing | *"2-ship Adder11"* | the example is a two-ship throughout |
| *"rolling off runway 25"* | *"runway 25 vacated"* | standard phraseology |
| *"Tower on 7, Adder11"* (single-aircraft form) | *"Adder1, channel 7, push"* | the example is a 2-ship, so use the flight form |
| (no pattern calls on the Mission Procedures dialog) | *"Adder11, in the break"* then *"turning final"* | MA Landing Procedures: call in the break if traffic; turning final is good practice. Optional — "on final" still gets the landing clearance |
| *"Control, Adder11, two-ship Hornets, inbound…"* | *"Adder11, two-ship Hornets, inbound…"* | Control was already contacted; no re-address |

Everything else is verbatim from the wiki.

## Regenerate

    cd scripts
    uv run --with pillow kneeboard.py --out ../kneeboard

Options: `--width`, `--height`, `--quality`, `--out`. The layout auto-fits the
page (the body shrinks to fit if a page is dense).

## Install into DCS

Copy the JPGs into your kneeboard folder, e.g.

    C:\Users\YOU\Saved Games\DCS.openbeta\Kneeboard

They then appear in the in-game kneeboard (cycle with the kneeboard keys).

## Editing the wording

The dialogs live in `PAGES` in `scripts/kneeboard.py` — edit them there and
regenerate. Keep them verbatim from the wiki so the cards stay authoritative.
