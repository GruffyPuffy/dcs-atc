# Kneeboard pages

Glanceable phraseology cards generated from the **exact** Master Arms SOP dialogs
(https://wiki.masterarms.se/index.php/Mission_Procedures), for use in the DCS
kneeboard during training.

| Page | Covers |
| --- | --- |
| `01-start-takeoff.jpg` | Ground (Ch 6) + Tower (Ch 7): check-in, ATIS, clearance, taxi, line-up, takeoff, handoff to Control |
| `02-rtb-landing.jpg` | Control (Ch 8) + Tower (Ch 7) + Ground (Ch 6): inbound, join, descent, entry, break, landing, taxi to parking |
| `03-airborne.jpg` | AWACS / package (not flown in the sim): check-in, push, attack, RTB handoff |

Each line is a request/answer exchange from the wiki (the "Adder11" two-ship
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
  flight** on the new frequency (*"Adder1" / "2"*), then make the first call
  with the agency prefix (*"Tower, Adder11, Entry East"*).

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
