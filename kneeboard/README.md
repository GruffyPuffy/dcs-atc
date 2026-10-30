# Kneeboard pages

Glanceable phraseology cards generated from the **exact** Master Arms SOP dialogs
(https://wiki.masterarms.se/index.php/Mission_Procedures), for use in the DCS
kneeboard during training. Page 5 is the Master Arms **comms board** (the UHF
channel plan from https://wiki.masterarms.se/index.php/Comms).

| Page | Covers |
| --- | --- |
| `01-start-takeoff.jpg` | Ground (Ch 6) + Tower (Ch 7): check-in, ATIS, clearance, taxi, line-up, takeoff, handoff to Control |
| `02-rtb-landing.jpg` | Control (Ch 8) + Tower (Ch 7) + Ground (Ch 6): inbound, join, descent, entry, break, landing, taxi to parking |
| `03-airborne.jpg` | AWACS / Stingray (Ch 3): check-in, bullseye picture, RTB, handoff back to Control |
| `04-light-path.jpg` | **Single-ship light path**: skip Ground (straight to Tower) and the overhead break (report "on final"); Tower → Control → land |
| `05-comms.jpg` | **Comms board**: the Master Arms UHF channel plan (Ch 1–25), with the channels this trainer uses highlighted |
| `06-brevity.jpg` | **Brevity**: the Master Arms radio words used on the ATC and package channels |

Page 4 is a **shortened trainer path**, not a wiki transcript: it uses the ATC
bot's actual replies to show the fewest calls that get a single-ship airborne and
back. Stuck at any point? Say *"Trainer, <callsign>, help"* on any frequency.

Page 5 is the **comms board** — the Master Arms UHF channel plan, verbatim from
the wiki's Comms page. The channels this trainer uses (Ch 6 Ground, Ch 7 Tower,
Ch 8 Control, Ch 21 ATIS) are highlighted in orange.

Each other line is based on the wiki's "Adder11" two-ship example, colour-coded
by who is talking (PILOT / GND / TWR / CTL / AWACS).

**Radio convention shown on the cards** (tidied from the SOP for training):

- Identify yourself on **every** line — the callsign is the only "caller ID".
- **First call** on a frequency addresses the agency (*"Ground, Adder11"*);
  after that just the callsign (*"Adder11, requesting taxi"*).
- On a **readback**, say the content first and the callsign **last**
  (*"Cleared taxi Sierra Echo and hold short Runway 25, Adder11"*).
- **Controllers** address the pilot on every line (*"Adder11, …"*) and name
  their own facility only on first contact.
- **Flight callsign vs individual callsign**: *Adder1* identifies the flight.
  Use it for the flight's channel push and its check-in after switching
  frequency (*"Adder1"*, then wingmen answer *"2"*, *"3"*, *"4"* …). Those
  check-in replies are internal to the flight; ATC does not answer them. The
  lead of any multi-ship flight (two-, three-, four-ship, etc.) uses their
  individual callsign (*Adder11*) when calling an agency, including when
  speaking for the whole flight (*"Tower, Adder11, Entry East"*). Other
  elements use their own numbers (*Adder12*, *Adder13*, …). The final digit is
  the element number: every flight lead ends in *1* (*Adder11*, *Adder21*,
  *Adder31*, *Adder41* for flights 1–4). A single-ship flight uses its flight
  callsign (*Adder1*, *Adder2*, etc.) for both flight and pilot calls.
  Flight-level calls such as *"Adder1, channel 7, push"*, *"Adder1 pushing"*,
  and *"Adder1 Miller Time"* remain flight calls.

Those are the pilot's transmission forms. The bot's reply address is set per
airfield in `airspace.json`: Kutaisi and Gudauta currently address the lead by
their individual callsign (for example, *Adder11*) even on a one-ship flight.

## Deliberate deviations from the wiki

The cards are the wiki dialogs with a few **intentional** corrections, so the
trainee learns the correct form rather than the wiki's slips:

| Wiki | Card | Why |
| --- | --- | --- |
| QNH readback: *"25 in use, QNH 2992."* (no callsign) | *"25 in use, QNH 2992, Adder11."* | every line must identify the sender |
| *"4-ship Adder11"* on the break/landing | *"2-ship Adder11"* | the example is a two-ship throughout |
| *"Adder1, two-ship Hornets on Ramp South"* | *"Adder11, two-ship Hornets on Ramp South"* | the lead uses their individual callsign when contacting Ground on behalf of the flight |
| *"rolling off runway 25"* | *"runway 25 vacated"* | standard phraseology |
| *"Tower on 7, Adder11"* (single-aircraft form in the wiki's two-ship example) | Two-ship cards: *"Adder1, channel 7, push"*; single-ship card: *"Tower on 7, Adder1"* | pilot transmission forms are separate from the bot's configured reply address |
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
regenerate. Keep the distinction between flight callsign and individual
callsign consistent with the wiki; intentional wording corrections are listed
above.
