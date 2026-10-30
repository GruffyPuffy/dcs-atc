"""Generate DCS kneeboard pages from the Master Arms SOP dialogs.

Renders three JPG pages with the **exact** request/answer exchanges from the
Master Arms wiki (https://wiki.masterarms.se/index.php/Mission_Procedures), so a
pilot can glance at the correct phraseology in the cockpit:

    1. Start / Takeoff   — Ground + Tower (check-in, clearance, taxi, departure)
    2. RTB / Landing     — Control + Tower + Ground (inbound, break, landing, parking)
    3. Airborne          — AWACS/Stingray (check-in, bullseye, RTB) — flown by the trainer

The first three dialogs follow the Master Arms wiki's "Adder11" two-ship
example. For that multi-ship flight, the lead uses "Adder11" on agency calls
and "Adder1" for flight check-ins and coordinated pushes. The single-ship card
uses "Adder1" for both flight and pilot callsign. Edit `PAGES` below to change
the wording.

Usage:
    uv run --with pillow kneeboard.py                 # -> kneeboard/*.jpg
    uv run --with pillow kneeboard.py --out /tmp/kb   # custom output dir
    uv run --with pillow kneeboard.py --width 1024 --height 768

Drop the JPGs into your DCS kneeboard folder, e.g.
    C:\\Users\\YOU\\Saved Games\\DCS.openbeta\\Kneeboard
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------- Layout ----------

# Portrait: the DCS kneeboard is a portrait clipboard (also in VR).
WIDTH, HEIGHT = 768, 1024
MARGIN = 40
BG = (13, 17, 23)          # GitHub dark
FG = (230, 237, 243)       # near-white
MUTED = (139, 148, 158)
ACCENT = (240, 136, 62)    # orange (headers)
RULE = (48, 54, 61)

FOOTER = ("Identify yourself on every line. First call on a frequency addresses "
          "the agency; after that just your callsign. On a readback, content "
          "first, callsign last. Controllers address the pilot every line and "
          "name themselves only on first contact.")

# Role -> colour. P = pilot, W = wingman, the rest are agencies.
ROLE_COLOR = {
    "P": (88, 166, 255),      # pilot (blue)
    "W": (88, 166, 255),
    "GND": (63, 185, 80),     # ground (green)
    "TWR": (210, 153, 34),    # tower (amber)
    "CTL": (163, 113, 247),   # control (purple)
    "AWACS": (219, 109, 40),  # AWACS (orange)
}
ROLE_LABEL = {
    "P": "PILOT", "W": "WING", "GND": "GND", "TWR": "TWR",
    "CTL": "CTL", "AWACS": "AWACS",
}

FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/Library/Fonts"),
    Path("C:/Windows/Fonts"),
]


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


# ---------- Content (based on the MA wiki dialogs) ----------

PAGES = [
    {
        "file": "01-start-takeoff",
        "title": "START / TAKEOFF",
        "subtitle": "Kutaisi — Ground (Ch 6) & Tower (Ch 7)",
        "sections": [
            ("Startup — check-in & ATIS", [
                ("P", "Adder1"),
                ("W", "2"),
                ("P", "Ground, Adder11"),
                ("GND", "Adder11, Ground"),
                ("P", "Adder11, two-ship Hornets on Ramp South (with information Charlie)"),
                ("GND", "Adder11, runway 25 in use, QNH 2992."),
                ("P", "25 in use, QNH 2992, Adder11."),
                ("GND", "Adder11, readback correct, advise when ready for clearance"),
            ]),
            ("Departure clearance", [
                ("P", "Adder11, ready to copy clearance"),
                ("GND", "Adder11, after departure turn right Exit North. 1500 ft or below"),
                ("P", "After departure turn right, Exit North, 1500 ft or below, Adder11"),
            ]),
            ("Taxi", [
                ("P", "Adder11, requesting taxi."),
                ("GND", "Adder11 cleared taxi Sierra Echo and hold short Runway 25."),
                ("P", "Cleared taxi Sierra Echo and hold short Runway 25, Adder11."),
                ("P", "Adder11 holding short, Runway 25"),
                ("GND", "Adder11, contact Tower on channel 7"),
                ("P", "Adder1, channel 7, push"),
            ]),
            ("Tower — line up & takeoff", [
                ("P", "Adder1"),
                ("W", "2"),
                ("P", "Tower, Adder11, at runway 25, ready for departure"),
                ("TWR", "Adder11, line up and wait runway 25"),
                ("P", "Line up and wait 25, Adder11"),
                ("TWR", "Adder11, right turnout, cleared for take-off runway 25"),
                ("P", "Right turn out, cleared for take-off, 25, Adder11"),
            ]),
            ("Take off & exit", [
                ("TWR", "Adder11, contact control on channel 8"),
                ("P", "Adder1, channel 8, push"),
                ("P", "Adder1"),
                ("W", "2"),
                ("P", "Control, Adder11, at 1500 ft"),
                ("CTL", "Adder11, radar contact, climb to Angels 15"),
                ("P", "Climbing to Angels 15, Adder11"),
                ("CTL", "Adder11, contact Stingray on channel 3"),
                ("P", "Adder1, channel 3, push"),
            ]),
        ],
    },
    {
        "file": "02-rtb-landing",
        "title": "RTB / LANDING",
        "subtitle": "Kutaisi — Control (Ch 8), Tower (Ch 7) & Ground (Ch 6)",
        "sections": [
            ("Returning to base — Control", [
                ("P", "Kutaisi Control, Adder11"),
                ("CTL", "Adder11, Control"),
                ("P", "Adder11, two-ship Hornets, inbound, 35 miles north of "
                      "Kutaisi at Angels 12, lowest state 4.5."),
                ("CTL", "Adder11, radar contact, turn left/right heading 150 to join via Entry East"),
                ("P", "150 to join via Entry East, Adder11"),
                ("CTL", "Adder11, descend to 1500 feet"),
                ("P", "Descend to 1500 feet, Adder11"),
                ("CTL", "Adder11, contact Tower on Channel 7"),
                ("P", "Adder1, channel 7, push"),
            ]),
            ("Tower — entry & break", [
                ("P", "Adder1"),
                ("W", "2"),
                ("P", "Tower, Adder11, Entry East"),
                ("TWR", "Adder11 report runway in sight"),
                ("P", "Report runway in sight, Adder11"),
                ("P", "Adder11, runway in sight"),
                ("TWR", "2-ship Adder11, wind 270, 2 knots, cleared for left overhead break runway 25"),
                ("P", "Cleared for left overhead break runway 25, Adder11"),
                ("P", "Adder11, in the break"),
                ("P", "Adder11, turning final"),
            ]),
            ("Landing", [
                ("P", "Adder11 on final"),
                ("TWR", "2-ship Adder11, wind 270, 2 knots, runway 25 cleared to land!"),
                ("P", "Runway 25, cleared to land, Adder11"),
            ]),
            ("Taxi to parking — Ground", [
                ("TWR", "Adder11, contact Ground on channel 6"),
                ("P", "Adder1, channel 6, push"),
                ("P", "Adder1"),
                ("W", "2"),
                ("P", "Ground, Adder11"),
                ("GND", "Adder11, Ground"),
                ("P", "Adder11, runway 25 vacated, requesting taxi to parking"),
                ("GND", "Adder11, cleared taxi to Ramp North via Alpha November"),
                ("P", "Cleared to Ramp North via Alpha November, Adder11"),
            ]),
        ],
    },
    {
        "file": "03-airborne",
        "title": "AIRBORNE",
        "subtitle": "AWACS / Stingray — check-in, bullseye picture, RTB",
        "note": "Stingray (AWACS) is a real controller in this trainer on the "
                "package channel (Ch 3). Authentication (RAMROD) is not yet "
                "implemented.",
        "sections": [
            ("Contacting AWACS", [
                ("P", "Adder1"),
                ("W", "2"),
                ("P", "Stingray, Adder11"),
                ("AWACS", "Adder11, Stingray"),
                ("P", "Adder11, Bullseye 065 for 40, Angels 15, as fragged"),
                ("AWACS", "Adder11, copy, authenticate Golf Alpha"),
                ("P", "Adder11 authenticates Romeo"),
                ("AWACS", "Adder11, radar contact"),
            ]),
            ("Holding & push", [
                ("P", "Adder1 pushing"),
            ]),
            ("Picture calls", [
                ("P", "Adder11, marking bull, 077 for 41"),
                ("P", "Adder11, marking Kutaisi 200 for 20, angels 5"),
            ]),
            ("The attack", [
                ("P", "Adder11, one away"),
                ("W", "Adder12, one away"),
                ("P", "Adder1 Miller Time, requesting RTB"),
                ("AWACS", "Adder11, copy, RTB"),
            ]),
            ("Handoff back to Control", [
                ("AWACS", "Adder11, Stingray"),
                ("P", "Stingray, Adder11"),
                ("AWACS", "Adder11, contact Kutaisi Control on Channel 8"),
                ("P", "Adder1, channel 8, push"),
            ]),
        ],
    },
    {
        "file": "04-light-path",
        "title": "LIGHT PATH",
        "subtitle": "Single-ship — minimal calls (skip Ground & the break)",
        "note": "Shortest way through the trainer: go straight to Tower (no Ground "
                "clearance/taxi) and report 'on final' (no overhead break). "
                "Stuck? Say 'Trainer, <callsign>, help' on any frequency.",
        "sections": [
            ("Takeoff — Tower (Ch 7)", [
                ("P", "Tower, Adder1, ready for departure"),
                ("TWR", "Adder1, line up and wait runway 25"),
                ("P", "Line up and wait 25, Adder1"),
                ("TWR", "Adder1, right turnout, cleared for takeoff runway 25"),
                ("P", "Right turn out, cleared for takeoff, 25, Adder1"),
                ("TWR", "Adder1, contact Control on channel 8"),
                ("P", "Control on 8, Adder1"),
            ]),
            ("Climb-out — Control (Ch 8)", [
                ("P", "Control, Adder1, at 1500 ft"),
                ("CTL", "Adder1, radar contact, climb to Angels 15"),
            ]),
            ("Return — Control (Ch 8)", [
                ("P", "Kutaisi Control, Adder1, inbound 35 miles north at Angels 12"),
                ("CTL", "Adder1, radar contact, turn right heading 150 to join via Entry East"),
                ("P", "150 to join via Entry East, Adder1"),
                ("CTL", "Adder1, descend to 1500 feet"),
            ]),
            ("Approach & landing — Tower (Ch 7)", [
                ("P", "Tower, Adder1, Entry East"),
                ("TWR", "Adder1, report runway in sight"),
                ("P", "Adder1, runway in sight"),
                ("TWR", "Adder1, wind calm, cleared for left overhead break runway 25"),
                ("P", "Cleared for left overhead break runway 25, Adder1"),
                ("P", "Adder1, on final"),
                ("TWR", "Adder1, runway 25, wind calm, cleared to land"),
                ("P", "Runway 25, cleared to land, Adder1"),
                ("TWR", "Adder1, contact Ground on channel 6"),
                ("P", "Ground on 6, Adder1"),
            ]),
        ],
    },
    {
        "file": "05-comms",
        "title": "COMMS",
        "subtitle": "Master Arms UHF channel plan (Com1 / PRI)",
        "note": "The default comms plan for missions. Mission-specific channels "
                "(Ch 13) fit the scenario and are listed in the mission event "
                "page. * = only in certain aircraft. ** = set per flight and "
                "starting airfield.",
        "table": [
            ("Ch", "Function", "Freq"),
            ("1", "Carrier Tower", "305.00"),
            ("2", "Carrier Tower (AI)", "264.00"),
            ("3", "Tactical", "265.00"),
            ("4", "Carrier Red Crown", "256.00"),
            ("5", "CAP A", "254.00"),
            ("6", "Ground", "250.00"),
            ("7", "Tower", "270.00"),
            ("8", "Control", "257.00"),
            ("9", "AWACS Check In", "255.00"),
            ("10", "CAP B", "262.00"),
            ("11", "Air Force Secondary Tanker (AI)", "259.00"),
            ("12", "Navy Secondary Tanker (AI)", "268.00"),
            ("13", "Mission Specific", "269.00"),
            ("14", "Navy Primary Tanker (AI)", "260.00"),
            ("15", "Carrier Approach A", "263.00"),
            ("16", "Carrier Marshal", "261.00"),
            ("17", "Carrier Approach B", "267.00"),
            ("18", "AWACS (AI)", "251.00"),
            ("19", "Carrier Overhead Tankers (AI)", "253.00"),
            ("20", "Air Force Primary Tanker (AI)", "266.00"),
            ("21*", "ATIS", "270.x0**"),
            ("22*", "Intraflight (Flight 1)", "xxx.xx**"),
            ("23*", "Intraflight (Flight 2)", "xxx.xx**"),
            ("24*", "Intraflight (Flight 3)", "xxx.xx**"),
            ("25*", "Tower (AI)", "xxx.xx**"),
        ],
        "highlight": {"3", "6", "7", "8", "21*"},
        "footer": "This trainer uses Ch 3 Stingray 265.00, Ch 6 Ground 250.00, "
                  "Ch 7 Tower 270.00, Ch 8 Control 257.00 and Ch 21 ATIS 270.50 "
                  "at every airfield.",
    },
    {
        "file": "06-brevity",
        "title": "BREVITY",
        "subtitle": "Radio words — Master Arms brevity (the ones you will hear)",
        "note": "The Master Arms brevity list, trimmed to the words used on the "
                "ATC and package channels. Full list: "
                "wiki.masterarms.se/index.php/Brevity.",
        "glossary": [
            ("Angels", "Height of a friendly aircraft in thousands of feet "
                       "(\"Angels 15\" = 15,000 ft)."),
            ("As fragged", "Will do exactly as briefed in the ATO — nothing has "
                           "changed, so no need to repeat it."),
            ("Authenticate", "Request or provide a response to a coded challenge "
                             "(RAMROD)."),
            ("Bingo", "Minimum fuel needed to return to base; also \"proceeding "
                      "to base\" — a reason-free way to say you are heading home."),
            ("Bogey", "A radar or visual contact whose identity is unknown."),
            ("Bandit", "A contact identified as enemy."),
            ("Bugout", "Leaving the engagement — no intent to re-engage."),
            ("Bullseye", "A fixed reference point; positions are given as "
                         "bearing and range from it (\"bullseye 065 for 40\")."),
            ("Buster", "Fly at maximum continuous speed."),
            ("Cease", "Break off the engagement on the specified target."),
            ("Clean", "No radar contacts; also no ordnance remaining."),
            ("Cleared", "The requested action is authorised."),
            ("Cold", "Attack geometry that passes behind the target; also "
                     "nose away from the threat."),
            ("Contact", "Sensor contact at the stated position."),
            ("Continue", "Continue the present manoeuvre — not a clearance to "
                         "engage."),
            ("Declare", "Request the identification of a specified track."),
            ("Divert", "Proceed to an alternate mission or base."),
            ("Estimate", "An estimate of size, range or height of a contact."),
            ("Faded", "Radar contact is lost."),
            ("Friendly", "A positively identified friendly contact."),
            ("Gate", "Fly as fast as possible, using afterburner."),
            ("Group", "Radar targets within about 3 NM of each other."),
            ("Heavy", "A group or package of three or more aircraft."),
            ("High", "Target altitude above the stated band."),
            ("Hold fire", "Stop firing on the designated target."),
            ("Hot", "Attack geometry that rolls out in front of the target; "
                    "also armed/live."),
            ("Joker", "Fuel state above bingo at which to start heading home."),
            ("Knock it off", "Cease the air-combat manoeuvring."),
            ("Locked", "Final radar lock-on."),
            ("Lost contact", "Radar contact lost."),
            ("Low", "Target altitude below the stated band."),
            ("Magnum", "Launch of a friendly anti-radiation missile."),
            ("Marking", "A position call naming the reference point — the "
                        "bullseye (\"marking bull, 077 for 41\") or another "
                        "point (\"marking Kutaisi 200 for 20\")."),
            ("Miller Time", "Ordnance delivered — the strike is complete; "
                            "usually with a request to RTB."),
            ("Monitor", "Maintain sensor awareness on the specified group."),
            ("No joy", "No contact; have not found the target."),
            ("Picture", "A request for, or a report of, the tactical situation."),
            ("Push", "Switch to the stated channel or frequency."),
            ("Radio check", "Request for a readability report."),
            ("Raygun", "A radar lock on a friendly aircraft (training)."),
            ("Readback", "Repeat the clearance back to the controller."),
            ("Roger", "I have received and understood your last transmission."),
            ("RTB", "Return to base."),
            ("Say again", "Repeat your last transmission."),
            ("Spike", "RWR indication of a radar lock on you."),
            ("Squawk", "Set the stated transponder code."),
            ("State", "Report your fuel state (\"lowest state 4.5\")."),
            ("Tally", "Sight of a target or enemy."),
            ("Traffic", "Aircraft in the area, friendly or unknown."),
            ("Unable", "Cannot comply with the request."),
            ("Vector", "A heading to steer, given by the controller."),
            ("Visual", "Sight of a friendly aircraft or ground position."),
            ("Wilco", "Will comply with your last instruction."),
            ("Winchester", "No ordnance remaining."),
        ],
        "footer": "Brevity is code words that shorten radio calls and reduce "
                  "mishearing. Use them only when both sides know them.",
    },
]


# ---------- Rendering ----------

def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _draw_table(d: ImageDraw.ImageDraw, page: dict, width: int, y: int,
                scale: float) -> int:
    """Draw a channel table (Ch | Function | Freq) and return the new y.

    Rows whose channel is in `page["highlight"]` are drawn in the accent colour
    and bold, so the channels this trainer actually uses stand out.
    """
    rows = page["table"]
    f_head = _font("DejaVuSans-Bold.ttf", max(10, int(19 * scale)))
    f_row = _font("DejaVuSans.ttf", max(10, int(18 * scale)))
    f_row_b = _font("DejaVuSans-Bold.ttf", max(10, int(18 * scale)))
    row_h = int(26 * scale)
    highlight = page.get("highlight") or set()

    # Column x positions: channel, function, frequency (right-aligned).
    x_ch = MARGIN
    x_fn = MARGIN + int(70 * scale)
    x_fr = width - MARGIN

    for i, (ch, fn, freq) in enumerate(rows):
        hot = ch in highlight
        font = f_head if i == 0 else (f_row_b if hot else f_row)
        color = ACCENT if (i == 0 or hot) else FG
        d.text((x_ch, y), ch, font=font, fill=color)
        d.text((x_fn, y), fn, font=font, fill=color)
        d.text((x_fr, y), freq, font=font, fill=color, anchor="ra")
        y += row_h
        if i == 0:
            d.line([(MARGIN, y - 4), (width - MARGIN, y - 4)], fill=RULE, width=1)
    return y + 12


def _measure(draw: ImageDraw.ImageDraw, page: dict, width: int,
             scale: float) -> int:
    """Total pixel height the page needs at a given scale (for auto-fit)."""
    f_head = _font("DejaVuSans-Bold.ttf", max(10, int(22 * scale)))
    f_text = _font("DejaVuSans.ttf", max(10, int(19 * scale)))
    f_note = _font("DejaVuSans-Oblique.ttf", max(9, int(16 * scale)))
    f_foot = _font("DejaVuSans-Oblique.ttf", 15)
    line_h = int(24 * scale)
    text_x = MARGIN + 74
    text_w = width - MARGIN - text_x

    y = MARGIN + 50 + 30 + 16  # title + subtitle + rule
    if page.get("note"):
        y += len(_wrap(draw, page["note"], f_note, width - 2 * MARGIN)) * 22 + 6
    if page.get("table"):
        y += len(page["table"]) * int(26 * scale) + 12
    if page.get("glossary"):
        # Two columns; approximate the taller one.
        f_term = _font("DejaVuSans-Bold.ttf", max(9, int(16 * scale)))
        f_def = _font("DejaVuSans.ttf", max(9, int(15 * scale)))
        col_gap = int(24 * scale)
        col_w = (width - 2 * MARGIN - col_gap) // 2
        heights = [len(_wrap(draw, de, f_def, col_w - 4)) * int(19 * scale)
                   + int(28 * scale) for _t, de in page["glossary"]]
        y += max(sum(heights[:len(heights) // 2]),
                 sum(heights[len(heights) // 2:])) + 12
    for heading, lines in page.get("sections", []):
        y += int(30 * scale)
        for _role, text in lines:
            y += len(_wrap(draw, text, f_text, text_w)) * line_h + 2
        y += int(12 * scale)
    # reserve room for the footer at the bottom
    footer = page.get("footer", FOOTER)
    y += len(_wrap(draw, footer, f_foot, width - 2 * MARGIN)) * 20 + 24
    return y


def _draw_glossary(d: ImageDraw.ImageDraw, page: dict, width: int, y: int,
                   scale: float) -> int:
    """Two-column term/definition list (the brevity page).

    Terms are drawn in the accent colour, definitions in the body colour, so a
    pilot can scan for a word quickly. Two columns keep a long list on one page.
    """
    f_term = _font("DejaVuSans-Bold.ttf", max(9, int(16 * scale)))
    f_def = _font("DejaVuSans.ttf", max(9, int(15 * scale)))
    entries = page["glossary"]
    col_gap = int(24 * scale)
    col_w = (width - 2 * MARGIN - col_gap) // 2
    # Split into two balanced columns by measured height, not by count, so a
    # few long definitions do not overflow one side.
    heights = []
    for term, definition in entries:
        h = len(_wrap(d, definition, f_def, col_w - 4)) * int(19 * scale) \
            + int(22 * scale)
        heights.append(h)
    total = sum(heights)
    target = total / 2
    split, acc = len(entries), 0
    for i, h in enumerate(heights):
        if acc + h > target:
            split = i
            break
        acc += h
    columns = [entries[:split], entries[split:]]
    for ci, col in enumerate(columns):
        x = MARGIN + ci * (col_w + col_gap)
        cy = y
        for term, definition in col:
            d.text((x, cy), term, font=f_term, fill=ACCENT)
            cy += int(22 * scale)
            for line in _wrap(d, definition, f_def, col_w - 4):
                d.text((x, cy), line, font=f_def, fill=FG)
                cy += int(19 * scale)
            cy += int(6 * scale)
    # Height of the taller column.
    return y + max(
        sum(len(_wrap(d, de, f_def, col_w - 4)) * int(19 * scale)
            + int(28 * scale) for _t, de in col)
        for col in columns)


def render_page(page: dict, width: int, height: int) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # Auto-fit: shrink the body until the content fits the page.
    scale = 1.0
    while scale > 0.6 and _measure(d, page, width, scale) > height - MARGIN:
        scale -= 0.02

    f_title = _font("DejaVuSans-Bold.ttf", 40)
    f_sub = _font("DejaVuSans.ttf", 19)
    f_head = _font("DejaVuSans-Bold.ttf", max(10, int(22 * scale)))
    f_role = _font("DejaVuSans-Bold.ttf", max(9, int(15 * scale)))
    f_text = _font("DejaVuSans.ttf", max(10, int(19 * scale)))
    f_note = _font("DejaVuSans-Oblique.ttf", max(9, int(16 * scale)))
    line_h = int(24 * scale)

    y = MARGIN
    d.text((MARGIN, y), page["title"], font=f_title, fill=FG)
    y += 50
    d.text((MARGIN, y), page["subtitle"], font=f_sub, fill=MUTED)
    y += 30
    d.line([(MARGIN, y), (width - MARGIN, y)], fill=RULE, width=2)
    y += 16

    if page.get("note"):
        for line in _wrap(d, page["note"], f_note, width - 2 * MARGIN):
            d.text((MARGIN, y), line, font=f_note, fill=MUTED)
            y += 22
        y += 6

    role_w = 74
    text_x = MARGIN + role_w
    text_w = width - MARGIN - text_x

    if page.get("table"):
        y = _draw_table(d, page, width, y, scale)
    if page.get("glossary"):
        y = _draw_glossary(d, page, width, y, scale)

    for heading, lines in page.get("sections", []):
        d.text((MARGIN, y), heading, font=f_head, fill=ACCENT)
        y += int(30 * scale)
        for role, text in lines:
            color = ROLE_COLOR.get(role, FG)
            d.text((MARGIN, y + 2), ROLE_LABEL.get(role, role), font=f_role, fill=color)
            wrapped = _wrap(d, text, f_text, text_w)
            for line in wrapped:
                d.text((text_x, y), line, font=f_text, fill=FG)
                y += line_h
            y += 2
        y += int(12 * scale)

    # Footer: the radio-discipline rule, pinned to the bottom.
    f_foot = _font("DejaVuSans-Oblique.ttf", 15)
    foot_lines = _wrap(d, page.get("footer", FOOTER), f_foot, width - 2 * MARGIN)
    fy = height - MARGIN - len(foot_lines) * 20
    d.line([(MARGIN, fy - 10), (width - MARGIN, fy - 10)], fill=RULE, width=1)
    for line in foot_lines:
        d.text((MARGIN, fy), line, font=f_foot, fill=MUTED)
        fy += 20

    return img


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="kneeboard",
                    help="output directory (default: kneeboard/)")
    ap.add_argument("--width", type=int, default=WIDTH)
    ap.add_argument("--height", type=int, default=HEIGHT)
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for page in PAGES:
        img = render_page(page, args.width, args.height)
        path = out / f"{page['file']}.jpg"
        img.save(path, "JPEG", quality=args.quality)
        print(f"wrote {path}  ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
