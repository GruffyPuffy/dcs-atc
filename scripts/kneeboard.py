"""Generate DCS kneeboard pages from the Master Arms SOP dialogs.

Renders three JPG pages with the **exact** request/answer exchanges from the
Master Arms wiki (https://wiki.masterarms.se/index.php/Mission_Procedures), so a
pilot can glance at the correct phraseology in the cockpit:

    1. Start / Takeoff   — Ground + Tower (check-in, clearance, taxi, departure)
    2. RTB / Landing     — Control + Tower + Ground (inbound, break, landing, parking)
    3. Airborne          — AWACS (check-in, push, attack, RTB) — not flown in the sim

The dialogs are transcribed verbatim from the wiki (the "Adder11" two-ship
example). Edit `PAGES` below to change the wording; the layout is automatic.

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


# ---------- Content (verbatim from the MA wiki) ----------

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
                ("P", "Adder1, two-ship Hornets on Ramp South (with information Charlie)"),
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
                ("P", "Adder1 holding short, Runway 25"),
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
        "subtitle": "AWACS / package — not flown in the sim (reference only)",
        "note": "AWACS and package comms are outside the ATC trainer; kept here for "
                "completeness of the MA SOP.",
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
    for heading, lines in page["sections"]:
        y += int(30 * scale)
        for _role, text in lines:
            y += len(_wrap(draw, text, f_text, text_w)) * line_h + 2
        y += int(12 * scale)
    # reserve room for the footer at the bottom
    y += len(_wrap(draw, FOOTER, f_foot, width - 2 * MARGIN)) * 20 + 24
    return y


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

    for heading, lines in page["sections"]:
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
    foot_lines = _wrap(d, FOOTER, f_foot, width - 2 * MARGIN)
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
