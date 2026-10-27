"""Rules-based ATC brain: recognize intents, produce canned ATC replies.

Deliberately dumb and deterministic — no LLM. Regexes tolerate the STT errors
we see in practice ("cold one" for "Colt 1", "Kutais/Kutasi" for "Kutaisi").

Reply wording lives in `phraseology.json` (templates with {placeholders}), so it
can be tuned to a community's SOP without touching this file. The *logic* — which
call triggers which intent, and the per-pilot state machine — stays here.

The brain is **controller-aware**: Ground, Tower and Control each answer their
own calls (see ATC.md §11). The bot maps the frequency a transmission arrived on
to a controller and passes it to `handle()`.

When a live position is available (from the DCS state bridge), replies become
distance-aware: an inbound call outside the CTR gets "report entering the
control zone", inside the CTR gets a radar-contact + downwind clearance.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import coach
import topgun
from atis import wind_speed_kt
from callsigns import CallsignRegistry, flight_name
from ctr import AircraftTrack, CtrEvent
from airspace import GATE_ARC_HALF_ANGLE_DEG, bearing_distance
from state_client import canonical_callsign, slot_callsign

TOWER = "Kutaisi Tower"
RUNWAY = "25"
DEFAULT_PHRASEOLOGY = Path(__file__).with_name("phraseology.json")

# How close (NM) a pilot must be to a named entry gate for an "at the entry"
# report to be accepted. Beyond this, Control challenges the position instead of
# handing the flight to Tower (a pilot who is not actually at the entry point
# should not be handed off).
ENTRY_REPORT_RADIUS_NM = 5.0
EXIT_REPORT_RADIUS_NM = 1.5  # retained for legacy map clients

# Arrival descent is stepped by distance to the entry gate, like a real
# controller (not a single canned "descend to 1500" at 35 miles). Within
# `DESCENT_FINAL_NM` of the entry -> cleared down to the final altitude (1500
# ft); further out -> an intermediate step-down (5000 ft) first.
DESCENT_FINAL_NM = 15.0
DESCENT_INTERMEDIATE_FT = 5000
# Above this altitude the final descent (1500 ft) is never issued directly,
# even inside the distance gate: the pilot gets the 5000 step-down first.
# An arrival abeam the entry gate can be "close" in a straight line while
# still high (debrief 2026-10-18 21:17:59: 6.5 NM north at Angels 14).
DESCENT_STEPDOWN_MAX_FT = 10000
# Pattern altitude Control descends an arrival to, and the slop for "you are
# there" (altimeter / radar, not a hard deck).
PATTERN_ALT_FT = 1500
PATTERN_ALT_SLOP_FT = 200
# The radar-driven landing clearance is a BACKSTOP for a silent pilot: it
# must fire EARLY enough to be useful — before the typical "on final" call
# point (~2 NM), not at it. A post-break final is flown from ~3 NM, so 3 NM
# gives the voice call the whole final to happen (and win), while a silent
# pilot is cleared with ~1 minute to run before the threshold (debrief
# 2026-10-18 21:24: the 2 NM backstop would have fired during the flare).
RADAR_CLEAR_NM = 3.0
# A pilot's spoken "on final" is checked primarily against the final-approach
# wedge. Allow a wide heading spread so wind correction and small alignment
# inputs do not provoke a false position challenge; the radar landing-clearance
# backstop keeps its separate, stricter established-final check.
ON_FINAL_REPORT_HEADING_TOLERANCE_DEG = 90.0
# Departure climb target (Angels 15) and slop for the unprompted Stingray
# handoff. Control does not simulate AWACS, but MA SOP still pushes the flight
# to Stingray on channel 3 once they are there.
ANGELS_HANDOFF_FT = 15000
ANGELS_HANDOFF_SLOP_FT = 500
# Master Arms describes the RTB handoff as usually occurring 25–80 NM from
# base. Use a single midpoint trigger; a pilot can still request Control early.
RTB_CONTROL_HANDOFF_NM = 50.0
RTB_CONTROL_HANDOFF_DELAY_SEC = 5.0

# Bullseye picture-call tolerance. The pilot reads bullseye off their HSI and
# STT mangles numbers, so a call is only "off" when it is clearly wrong; an off
# call is coached, never blocked (the flight continues either way).
BULLSEYE_TOLERANCE_DEG = 10.0
BULLSEYE_TOLERANCE_NM = 3.0
# Angels tolerance for a picture call, in feet (the call states thousands of
# feet, so 1000 ft = 1 angel). Generous: the pilot reads it off the HUD.
ANGELS_TOLERANCE_FT = 1000.0

# How many consecutive polls with the aircraft on the runway before the
# "vacate immediately" incursion warning fires (~2 s poll cadence -> 10 s).
# A pilot creeping forward to line up right after the Ground->Tower handoff
# clips the pavement briefly; warning them then is noise, not safety.
INCURSION_POLLS = 5

# Spoken number words -> digits. STT writes what it *hears*, so a clearance
# figure arrives as words ("runway seven", "QNH two nine nine six") while every
# intent regex is written for the digits a DCS pilot reads off the cockpit.
# Covers digit words, teens and tens so compound altides ("angels thirty two")
# parse as 32, not as 302.
_ONES = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
         "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
         "niner": "9"}
_TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
          "fourteen": "14", "fifteen": "15", "sixteen": "16",
          "seventeen": "17", "eighteen": "18", "nineteen": "19"}
_TENS = {"twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
         "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90"}
_NUMBER_WORDS = {**_ONES, **_TEENS, **_TENS}

# The words a figure may be built from, longest first so "twenty" is preferred
# over a hypothetical "two" prefix match inside it.
_FIGURE_WORD = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

# A spoken figure after a number context: a runway, channel, heading or
# altitude. Both the digit-by-digit form ("heading one four four" -> 144) and
# the compound form ("angels thirty two" -> 32) are captured, with an optional
# trailing "thousand" ("flight level two four zero", "angels two one thousand").
_FIGURE_CONTEXT_RE = re.compile(
    r"\b(runway|channel|heading|angels|altitude|flight level)\s+"
    rf"((?:(?:{_FIGURE_WORD})\s*){{1,4}}(?:thousand)?)",
    re.IGNORECASE)


def _spoken_figure(phrase: str) -> str:
    """Parse a spoken figure ("one five", "thirty two", "fifteen") to digits.

    Returns "" when the phrase is not a clean number. A trailing "thousand"
    multiplies by 1000 ("two one thousand" -> "21000").
    """
    words = phrase.split()
    scale = ""
    if words and words[-1] == "thousand":
        words, scale = words[:-1], "000"
    if not words or any(w not in _NUMBER_WORDS for w in words):
        return ""
    # Compound tens+units ("thirty two") is one *value*; a trailing tens/teens
    # word is the last two digits ("four forty" -> 440, "one forty" -> 140,
    # "fifteen" -> 15); anything else is digit-by-digit ("one five" -> 15,
    # "zero seven zero" -> 070). Every word is known to be a number word here,
    # so `_NUMBER_WORDS` is safe for the leading digits too — a teen in the
    # middle ("fifteen seven zero three") used to raise KeyError and kill the
    # reply entirely, and "heading one forty" (a very common heading call) hit
    # the same crash.
    if len(words) >= 2 and words[-2] in _TENS and words[-1] in _ONES:
        head = [_NUMBER_WORDS[w] for w in words[:-2]]
        digits = "".join(head) + str(int(_TENS[words[-2]])
                                    + int(_ONES[words[-1]]))
    elif words[-1] in _TENS or words[-1] in _TEENS:
        digits = "".join(_NUMBER_WORDS[w] for w in words[:-1]) \
            + _NUMBER_WORDS[words[-1]]
    else:
        digits = "".join(_NUMBER_WORDS[w] for w in words)
    return digits + scale


def _fold_spoken_figures(text: str) -> str:
    """Fold spoken number words into figures where the context is a number.

    "runway zero seven" -> "runway 07", "channel three" -> "channel 3",
    "channel two five seven" -> "channel 257", "heading one four four" ->
    "heading 144", "angels thirty two" -> "angels 32".

    This is the post-STT normalisation the intent regexes want: they are all
    written for figures (``\\bchannel\\s*\\d\\b``), because DCS pilots read
    *digits* off the cockpit, but STT transcribes the *words*. Folding the
    words to figures at one choke point removes a whole class of "say again"
    for numbers we already know how to handle (debrief 2026-10-22: every
    channel readback was rejected by a digits-only test, and "angels fifteen"
    compared against a "15" clearance as if the pilot had said nothing).

    It is deliberately **context-gated** rather than a blanket word->digit
    substitution: a global replace would rewrite "one" in "the one on final"
    and destroy the formation ("two-ship") and flight-check-in ("two") word
    forms. The flight check-in runs on the original text, before this.
    """
    def merge(match: "re.Match") -> str:
        figure = _spoken_figure(match.group(2))
        if not figure:
            return match.group(0)
        # Runway designators keep their leading zero ("zero seven" -> "07");
        # every other figure is a plain number ("angels fifteen" -> "15").
        if match.group(1).lower() == "runway":
            figure = figure.zfill(2) if len(figure) < 2 else figure
        else:
            figure = figure.lstrip("0") or "0"
        # Replace only the figure words, leaving the surrounding text — and in
        # particular the whitespace after them — untouched. Rebuilding the whole
        # match welded "7" onto the next word ("channel seven push" became
        # "channel 7push"), so the channel regex rejected a valid readback
        # (debrief 2026-10-22, 22:06:45).
        # Group 2 is the figure words plus any trailing whitespace the regex
        # allowed; keep that whitespace so the next word stays separate.
        spoken = match.group(2)
        trailing = spoken[len(spoken.rstrip()):]
        return match.group(1) + " " + figure + trailing

    return _FIGURE_CONTEXT_RE.sub(merge, text)


# A bullseye or marking call's figures, folded as one unit. The bearing follows
# the point name ("marking Kutaisi two zero zero") or the keyword itself
# ("bullseye zero six five"); the range follows "for"/"at" — or just a comma, as
# in Mags' "marking bull, zero seven seven, four forty". Folded separately from
# the general context above because "for" is far too common a word to fold
# globally; this only runs when the transmission is a bullseye/marking call.
# The point name is any word that is *not* a number word, so "bullseye zero six
# five" does not read "zero" as the point. Bearing and range are folded in one
# pass so the range cannot be mistaken for the bearing (a separate range pass
# would match the comma *before* the bearing and strip its leading zero).
_BULLSEYE_FOLD_RE = re.compile(
    rf"\b(bull'?s?eye|marking)((?:\s+(?!(?:{_FIGURE_WORD})\b)[a-z]+)?)"
    rf"([\s,]+)((?:(?:{_FIGURE_WORD})\s*){{1,4}})"
    rf"(?:(for|at|,)([\s,]*)((?:(?:{_FIGURE_WORD})\s*){{1,4}}))?",
    re.IGNORECASE)


def _fold_bullseye_figures(text: str) -> str:
    """Fold spoken figures in a bullseye/marking call.

    "bullseye zero six five for four zero" -> "bullseye 065 for 40",
    "marking Kutaisi two zero zero for twenty" -> "marking Kutaisi 200 for 20".

    Runs *after* `_fold_spoken_figures` (which already handles the trailing
    "angels five"), and only when the transmission is a bullseye/marking call —
    the range context ("for"/"at"/",") is far too common to fold globally.
    """
    if not re.search(r"\b(bull'?s?eye|marking)\b", text, re.IGNORECASE):
        return text

    def merge(match: "re.Match") -> str:
        out = match.group(1) + (match.group(2) or "") + match.group(3)
        for figure_group, sep_groups in ((4, (5, 6)), (7, ())):
            spoken = match.group(figure_group)
            if spoken is None:
                continue
            figure = _spoken_figure(spoken)
            if figure:
                # Keep the whitespace the regex allowed after the figure, so
                # the next word stays separate (the same trap as
                # `_fold_spoken_figures`).
                trailing = spoken[len(spoken.rstrip()):]
                # A bearing is always three digits ("zero six five" -> 065);
                # the range is a plain number ("four zero" -> 40).
                if figure_group == 4:
                    figure = figure.zfill(3)
                else:
                    figure = figure.lstrip("0") or "0"
                out += figure + trailing
            else:
                out += spoken
            out += "".join(match.group(i) or "" for i in sep_groups)
        return out

    folded = _BULLSEYE_FOLD_RE.sub(merge, text)
    # Whisper sometimes renders the spoken separator "for" as a stray 4,
    # leaving three comma-separated groups: "marking bulls, 63, 4, 46".
    # Without this repair parse_marking_call consumes "63, 4" as the BRA and
    # silently ignores the actual range (46). Restrict the repair to a
    # bullseye/marking phrase and keep the existing live-position verification
    # as the authority on whether the resulting BRA is correct.
    return re.sub(
        r"(\b(?:bulls?|bull'?s?eye|marking(?:\s+[a-z']+)?)[\s,]+"
        r"\d{2,3})\s*,\s*(?:4|four)\s*,\s*(\d{1,3})\b",
        r"\1 for \2", folded, flags=re.IGNORECASE)


def _stem(word: str) -> str:
    """A regex fragment matching a verb in its common spoken forms.

    STT transcribes inflections: "descending", "maintaining", "cleared".
    `\bword\b` misses them ("descend" does not match "descending"), so match
    the stem with optional suffixes instead.
    """
    return rf"\b{word}(?:ing|ed|s)?\b"


# A bullseye picture call: "bullseye 065 for 40, Angels 15" -> bearing, range
# and (optionally) angels. The bearing is a 2-3 digit group, the range follows
# "for"/"at", and angels is the altitude in thousands of feet. Module-level so
# the map server can parse the same call for its debug overlay (one parser, so
# the drawn arrow can never disagree with what the brain checked).
_BULLSEYE_CALL_RE = re.compile(
    r"\bbull'?s?eye\b\s*,?\s*(\d{2,3})\s*(?:for|at|,)\s*(\d{1,3})")
_ANGELS_RE = re.compile(r"\bangels\s*,?\s*(\d{1,3})\b")
# An exact-altitude statement: "angels 15703", "15703 feet", "15703 tf". The SOP
# wants whole thousands ("angels 16"); reading the digits off the altimeter is a
# habit the trainer should coach out (Mags: "Apollo blir irriterad om man läser
# siffrorna exakt"). Deliberately 4-5 digits, so it cannot collide with the
# 1-3 digit angels figure above.
_EXACT_ALT_RE = re.compile(
    r"\b(?:angels\s*(\d{4,5})|(\d{4,5})\s*(?:feet|foot|ft|tf)\b)")
# A marking call: "marking <point> <bearing> for <range>". The point is the
# bullseye ("bull"/"bulls"/"bullseye") or a named point ("Kutaisi"). The range
# separator is "for"/"at" or just a comma (Mags' "marking bull, 077, 440").
# Matched against the folded text, so spoken figures work too.
_MARKING_RE = re.compile(
    r"\bmarking\s+([a-z']+)\s*,?\s*(\d{2,3})\s*(?:for|at|,)\s*(\d{1,3})\b")
# The words a pilot uses for the bullseye itself, as opposed to a named point.
_BULLSEYE_WORDS = {"bull", "bulls", "bullseye", "bull's", "bulls'", "bullseye's"}


def parse_bullseye_call(text: str) -> tuple[float, float] | None:
    """Parse "bullseye 065 for 40" -> (bearing, range_nm), or None.

    Reads the folded text, so spoken numbers ("zero six five") work too.
    """
    match = _BULLSEYE_CALL_RE.search(text or "")
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _picture_bra_variants(bearing: float,
                          distance: float) -> list[tuple[float, float]]:
    """Return plausible BRA readings for STT joining "for" into a figure.

    Keep the literal reading first. STT can turn "for 57" into "457" and
    "for 5" into "45"; try removing that leading four from either BRA field.
    A candidate is only accepted when the live position verifies it within the
    normal tolerances, so legitimate 045 / 45 readings remain valid too.
    """
    canonical = (bearing, distance)
    choices = [canonical]
    bearings = [bearing]
    ranges = [distance]

    def without_joined_four(value: float) -> float | None:
        # Three digit form: "457" -> "57". Two digit form: "45" -> "5".
        if 400 <= value < 500:
            return value % 100
        if 40 <= value < 50:
            return value % 10
        return None

    bearing_variant = without_joined_four(bearing)
    range_variant = without_joined_four(distance)
    if bearing_variant is not None:
        bearings.append(bearing_variant)
    if range_variant is not None:
        ranges.append(range_variant)
    for candidate_bearing in bearings:
        for candidate_range in ranges:
            candidate = (candidate_bearing, candidate_range)
            if candidate not in choices:
                choices.append(candidate)
    return choices


def _picture_call_variants(text: str) -> list[tuple[float, float]]:
    """Read all plausible bullseye BRA values from a folded call."""
    canonical = parse_bullseye_call(text)
    return (_picture_bra_variants(*canonical) if canonical is not None else [])


def parse_marking_call(text: str) -> tuple[str, float, float] | None:
    """Parse "marking Kutaisi 200 for 20" -> ("kutaisi", 200.0, 20.0), or None.

    The point is returned lower-cased; "bull"/"bulls"/"bullseye" mean the
    coalition bullseye itself. Reads the folded text, so spoken figures work.
    """
    match = _MARKING_RE.search(text or "")
    if not match:
        return None
    point = match.group(1).lower()
    if point in _BULLSEYE_WORDS:
        point = "bullseye"
    return point, float(match.group(2)), float(match.group(3))


def parse_angels(text: str) -> float | None:
    """Parse "angels 15" -> 15.0 (thousands of feet), or None.

    Angels is the altitude in thousands of feet, so "angels 15" is 15,000 ft.
    """
    match = _ANGELS_RE.search(text or "")
    return float(match.group(1)) if match else None


def parse_exact_altitude(text: str) -> float | None:
    """Parse an exact-altitude statement -> feet, or None.

    "angels 15703", "15703 feet" and "15703 tf" all mean the pilot read the
    digits off the altimeter instead of rounding to whole thousands. Returns the
    stated altitude in feet so the trainer can coach the correct call.
    """
    match = _EXACT_ALT_RE.search(text or "")
    if not match:
        return None
    return float(match.group(1) or match.group(2))


# Spoken digits, for the frequency-change patterns. STT writes what it hears, so
# a channel is usually a *word* ("channel seven"), never a figure: a
# `channel\s*\d` guard silently missed every real readback (debrief 2026-10-22:
# "Stingray, channel three" and "pushing channel three" both got "say again").
_CHANNEL_NUMBER = r"(?:\d+|" + "|".join(_NUMBER_WORDS) + r")"
# A mention of an agency, for the bare check-in test ("Tower, Colt 1"). Matched
# against the agency names rather than the *own* agency so the message can also
# be a check-in on the wrong frequency.
_BARE_CHECKIN_RE = re.compile(
    r"\b(?:tower|ground|control|approach|departure|center|centre|atis)\b")
# A pilot acknowledging a frequency change: "channel 8, push", "channel seven
# tower", "pushing channel three", "switching to channel 3", "going to 257".
_CHANNEL_CHANGE_RE = re.compile(
    rf"\bchannel\s*{_CHANNEL_NUMBER}\b"
    rf"|\b(?:push(?:ing)?|switch(?:ing)?|chang(?:ing|e)|going|moving|"
    rf"contacting|diall?ing)\b[^.]*\bchannel\b"
    rf"|\bpush\b")
# A pilot *asking to change frequency*, rather than acknowledging one. A real
# controller answers this ("contact Stingray on channel 3"), instead of leaving
# the pilot to guess — this was 4 minutes of the 2026-10-22 sortie.
#
# Covers every way a pilot asks to move: naming a channel/frequency, naming an
# agency ("request Control", "request Tower", "request Stingray"), or asking
# generically ("request channel change", "when can I push"). This is a trainer:
# the pilot may ask to move to *any* agency from any agency, so the request verb
# is matched against any agency name rather than a hand-picked list.
_AGENCY_WORDS = (r"ground|tower|control|approach|departure|stingray|awacs|"
                 r"magic|overlord|center|centre")
_CHANNEL_REQUEST_RE = re.compile(
    rf"\b(?:request(?:ing)?|ask(?:ing)? for|like|need|want|would like|"
    rf"can i|when can i|permission to)\b[^.]*"
    rf"\b(?:channel|frequency|freq|push|switch|{_AGENCY_WORDS})\b"
    r"|\b(?:channel|frequency|freq)\s*(?:change|switch)\b"
    r"|\b(?:when can i|can i)\b[^.]*\bpush\b")
# Everything the pilot said that is *not* the call itself: the agency name, the
# callsign, the F-word of "F-word, calling" and punctuation. Used to tell a bare
# check-in ("Tower, Epic 4") from a real call ("Tower, Epic 4, requesting taxi").
_CALL_FILLER_RE = re.compile(
    r"\b(?:this is|calling|over)\b|[^a-z0-9]+")


class Controller(str, Enum):
    """Which controlling agency is being addressed (see ATC.md §11)."""

    GROUND = "ground"
    TOWER = "tower"
    CONTROL = "control"
    STINGRAY = "stingray"


class Phase(str, Enum):
    """Per-aircraft flight phase (see ATC.md §5)."""

    IDLE = "Idle"
    CLEARANCE = "Clearance"
    TAXI = "Taxi"
    HOLDING = "Holding"
    LINEUP = "Lineup"
    # Cleared for takeoff / rolling. Entered by the takeoff *readback*, i.e.
    # before the aircraft moves — hence "Takeoff", not "Departure" (the old name
    # made a Control check-in mis-classify a departure as an arrival).
    TAKEOFF = "Takeoff"
    AIRBORNE = "Airborne"
    # Handed to Stingray (AWACS) after the climb; the tactical leg before RTB.
    STINGRAY = "Stingray"
    INBOUND = "Inbound"
    LANDING = "Landing"


@dataclass
class PilotState:
    """Independent state for one callsign (multiplayer-safe)."""

    callsign: str
    phase: Phase = Phase.IDLE
    cleared_inbound: bool = False
    cleared_landing: bool = False
    # Tower has issued the overhead-break clearance (debrief 2026-10-18: the
    # pilot's readback "right overhead brake" must not re-trigger the request).
    break_cleared: bool = False
    # The aircraft has actually flown through a pattern turn slice (downwind/
    # base) since the break clearance — the radar OBSERVED the OHB turn, not
    # just heard about it. The radar-driven landing clearance requires this:
    # an arrival sitting on the centreline (straight-in geometry) must never
    # look "established on final" before it has broken (debrief 2026-10-18
    # 20:49:55: cleared to land 1 s after the RWIS report, 8 NM out).
    pattern_flown: bool = False
    go_around_issued: bool = False
    descend_issued: bool = False  # Control has issued the descent to 1500 ft
    descend_intermediate: bool = False  # Control issued the far-out step-down
    climb_issued: bool = False  # Control has issued the departure climb
    control_checkin_received: bool = False  # pilot checked in after handoff
    # Control has pushed the departure to Stingray (AWACS). Fires once; we do
    # not simulate channel 3, but the SOP call still goes out on Control.
    stingray_handoff_announced: bool = False
    altitude_warned: bool = False  # warned about busting the CTR ceiling
    incursion_warned: bool = False  # warned about being on the runway uncleared
    incursion_streak: int = 0  # consecutive on-runway polls (debounce)
    exit_gate: str = ""  # assigned departure exit point
    entry_gate: str = ""  # assigned arrival entry point
    vector_heading: str = ""  # latest assigned vector, used to consume readback
    vector_runway: str = ""
    departure_previous_range_nm: float | None = None
    departure_previous_bearing_deg: float | None = None
    arrival_previous_range_nm: float | None = None
    arrival_previous_bearing_deg: float | None = None
    # Held because the runway was occupied; the bot calls the pilot back when it
    # clears. `*_ready_announced` fires the unprompted "runway is clear" once.
    awaiting_takeoff: bool = False
    awaiting_landing: bool = False
    takeoff_ready_announced: bool = False
    landing_ready_announced: bool = False
    # The pilot has read back "line up and wait" but is not yet lined up on the
    # runway. The takeoff clearance waits until they are (see check_lineup).
    lineup_readback: bool = False
    # The pilot has read back the current descent clearance. This tracks the
    # descent sequence only; Tower handoff is driven by entry-arc/CTR position.
    descend_readback: bool = False
    # Tower has pushed a landed flight to Ground (Master Arms SOP: "after
    # landing, tower will push you to the Ground channel"). Fires once per
    # landing; re-arms on the next approach (go-around / touch-and-go).
    landing_handoff_announced: bool = False
    # Tower has pushed a departure to Control (MA SOP: "expect instructions to
    # switch to control" once airborne). Re-armed by each takeoff clearance.
    departure_handoff_announced: bool = False
    # The flight has told Stingray it is returning to base ("Miller Time",
    # "Bingo", "requesting RTB"). Arms the handoff back to Control.
    rtb_requested: bool = False
    rtb_control_handoff_announced: bool = False
    rtb_control_handoff_not_before: float = 0.0
    tower_handoff_announced: bool = False
    formation: int = 0  # flight size (2+ = multi-ship), learned from calls
    # Trainer aid: this pilot asked for the in-game comms mirror ("trainer,
    # echo on"). Per-pilot, so each pilot controls their own feed.
    mirror_logs: bool = False
    # Ground has issued the taxi-to-parking clearance. Guards against the
    # readback ("cleared taxi to ramp north") re-matching the parking request
    # (it contains "to ramp") and re-issuing the clearance forever.
    parking_cleared: bool = False
    last_reply: str = ""  # last clearance, replayed on "say again"
    last_clearance: str = ""  # last clearance issued, for readback verification
    last_controller: str = ""  # last agency talked to (for the map view)
    # Radio handoff state is explicit so the map/debrief can distinguish the
    # agency last heard, the agency ATC asked the pilot to contact, and an
    # agency the pilot requested.
    checked_in_at: str = ""
    pending_handoff_to: str = ""
    requested_handoff_to: str = ""
    readback_fails: int = 0  # consecutive incomplete readbacks of one clearance
    # Last transmission was judged as a READBACK (verified via _check_readback/
    # _readback_coaching), so the coach can attach readback-specific wording
    # notes (e.g. a callsign leading the readback). Reset on every call.
    last_readback: bool = False
    # Ground has read out the runway/QNH (or weather) info, so a later call
    # naming "in use"/QNH is a readback of it. Without this, any transmission
    # that merely mentions "in use" was answered "readback correct".
    info_issued: bool = False
    # Wording-coach note for the LAST transmission (coach mode only), to be
    # delivered as a direct in-game message (not spoken on the radio). Set by
    # AtcBrain.handle; the controller worker takes and clears it after the
    # call ("take_coach") — never replayed by "say again".
    pending_coach: str = ""


class Phraseology:
    """Reply templates loaded from phraseology.json."""

    def __init__(self, templates: dict[str, str], variables: dict[str, str]):
        self.templates = templates
        self.variables = variables

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PHRASEOLOGY) -> "Phraseology":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("templates", {}), data.get("variables", {}))

    def render(self, key: str, **fields: object) -> str:
        template = self.templates.get(key)
        if template is None:
            raise KeyError(f"no phraseology template {key!r}")
        values = {**self.variables, **fields}
        return template.format(**values)


class AtcBrain:
    """State machine per aircraft. Recognizes intents, returns reply text."""

    def __init__(self, tower: str = TOWER, runway: str = RUNWAY,
                 phraseology: Phraseology | None = None,
                 callsigns: CallsignRegistry | None = None,
                 ground: str = "Ground", control: str = "Control",
                 stingray: str = "Stingray",
                 gates: list[str] | None = None,
                 gate_locator=None, airfield=None):
        self.tower = tower
        self.runway = runway
        self.ground = ground
        self.control = control
        self.stingray = stingray
        # Master Arms: an agency identifies itself by ROLE ("Tower", "Ground",
        # "Control"), not the full name ("Kutaisi Tower"). The full name is only
        # used by the pilot when initiating contact. Replies use the short name.
        self.tower_short = tower.split()[-1] if tower else "Tower"
        self.ground_short = ground.split()[-1] if ground else "Ground"
        self.control_short = control.split()[-1] if control else "Control"
        self.stingray_short = stingray.split()[-1] if stingray else "Stingray"
        self.gates = gates or []
        self.gate_locator = gate_locator  # callable(lat, lon) -> gate name
        self.airfield = airfield  # Airfield, for position cross-checks
        self.phraseology = phraseology or Phraseology.load()
        self.callsigns = callsigns or CallsignRegistry()
        self.pilots: dict[str, PilotState] = {}
        self.wind = "calm"  # spoken phrase, updated from live weather
        self.wind_dir = 0.0  # meteorological FROM, degrees
        self.wind_speed = 0.0  # as reported by weather (m/s)
        self.qnh = "2992"  # updated from live weather (inHg, 4 digits)
        # Weather phrase ("CAVOK" / visibility + clouds), from live weather.
        # Ground reads it out when a pilot checks in WITHOUT "with information X"
        # (MA SOP: "weather is CAVOK. Wind 270, 2 knots, Runway 25 in use, ...").
        self.weather = ""
        # Coalition bullseye (main reference point) from the mission, as
        # (lat, lon). None until the bridge supplies it; a bullseye call is then
        # acknowledged without a position check rather than inventing a point.
        self.bullseye: tuple[float, float] | None = None
        # Cockpit bearing convention (see airspace.py): how the pilot's
        # instruments display a geodesic-true bearing. Bullseye calls are
        # reported and answered in cockpit units, and every heading the bot
        # speaks is given in cockpit units so the HSI shows what it asked for.
        # Internal math, the bridge and the web map stay geodesic.
        self.cockpit_bearing_offset_deg = -float(
            getattr(airfield, "magnetic_variation_deg", 0.0)
            + getattr(airfield, "map_rotation_deg", 0.0))
        # TTS output volume, adjustable at runtime with "trainer, volume 0.3".
        # None = use the --tx-volume default.
        self.tx_volume: float | None = None
        # Trainer mode (GLOBAL, like tx_volume — not per pilot). Coach (the
        # default) is the teaching persona: wording-suggestion talkback lives
        # here later. Casual is the same semantic standards but relaxed — no
        # wording suggestions, just ATC. Readback VALUES stay checked in both
        # (they are semantics, not wording); there is deliberately no
        # in-flight "strict" — strictness lives in the debrief scoring.
        self.mode = "coach"
        # Rotates the departure exit gate between flights (no traffic), so a
        # pilot practises different exits instead of always the straight-out one.
        self._exit_rotation = 0
        # SRS speaker name -> flight callsign, learned from transmissions, so
        # automatic calls (CTR warning, go-around) can address the pilot by
        # callsign instead of the raw DCS unit name.
        self.speaker_callsigns: dict[str, str] = {}
        # Speaker-map keys seeded by the last `sync_speakers` poll, with the
        # callsign seeded. A seed is dropped when the player leaves or respawns;
        # a radio-learned binding on a dynamic slot is dropped the same way.
        self._seeded_keys: dict[str, str] = {}
        # Last DCS unit name per player from `sync_speakers`. A change is a new
        # spawn: the previous callsign is forgotten so the new slot can bind.
        self._online_players: dict[str, str] = {}
        # Single-pilot training aid: when true, a transmission we do not
        # understand is prompted with "say again" (addressed to the one pilot)
        # instead of being ignored. Set from the live pilot count (see
        # `set_pilot_count`); a silent miss is confusing on a solo trainer.
        self.solo_mode = False
        # Callsigns currently online (from the state bridge). A single online
        # pilot whose flight is known becomes the fallback for a garbled call.
        self.active_pilots: list[str] = []
        # TopGun easter egg (env-gated, off by default; see topgun.py). This is
        # a pure side-channel: it never touches a pilot's phase. `_topgun_armed`
        # holds the callsigns who have asked to bust the tower, so the stunt can
        # be caught off-mic without changing the ATC state machine.
        self._topgun_armed: set[str] = set()

    def set_pilot_count(self, count: int) -> None:
        """Enable solo training mode when there is a single player online."""
        self.solo_mode = count <= 1

    def set_active_pilots(self, callsigns) -> None:
        """Record the callsigns currently online (from the live state bridge).

        Used as the candidate set for attributing a garbled transmission to a
        known pilot when no callsign could be extracted (see
        `_resolve_speaker`).
        """
        self.active_pilots = [c for c in (callsigns or []) if c]

    def topgun_armed(self, callsign: str) -> bool:
        """True if `callsign` has asked to bust the tower (easter egg)."""
        return callsign in self._topgun_armed

    def disarm_topgun(self, callsign: str) -> None:
        """Clear the TopGun arm flag (fire the stunt sting at most once)."""
        self._topgun_armed.discard(callsign)

    def remember_speaker(self, who: str, callsign: str) -> None:
        """Map an SRS speaker name to the callsign they used.

        A speaker name can only ever map to one flight callsign, so we key on a
        normalised form (a stale mapping would send *another* pilot's automatic
        calls to the wrong aircraft). Any extra spelling of the same pilot that
        the registry knows about is mapped to the same callsign, so a player
        whose SRS name matches a known STT variant is recognised too.

        The binding is set **once per DCS spawn**. STT cannot jump Caveman from
        Adder 1 to Adder 2 mid-flight; changing flight means respawning in
        another slot and checking in again.
        """
        if not who or not callsign:
            return
        current = self.callsign_for_speaker(who)
        if current and current != who:
            return
        for key in self.callsigns.speaker_spellings(who):
            self.speaker_callsigns[key] = callsign

    def sync_speakers(self, pairs: list[tuple[str, str]]) -> None:
        """Sync the callsign registry and speaker map from the live unit list.

        `pairs` is (player name, unit name) for every live player, as DCS
        reports them (`u:getPlayerName()` / `u:getName()`). DCS gives us the
        unit's *name*, never its radio callsign: for a mission slot the name
        often *is* the callsign ("Springfield11"), but on a user-made mission it
        can be unrelated ("Aerial-1-1") and a dynamically spawned slot gets a
        synthetic name ("Kutaisi_F/A-18C Lot 20_2- 1"). So the name is only
        trusted when `slot_callsign` can parse a callsign out of it; otherwise
        seeding would put the unit name on the air.

        This is the single entry point for keeping the brain in step with who is
        actually flying:

        - **Learn** flight names not in `env.mission` (dynamic slots), so a
          player who spawned into a dynamically created flight is recognised.
        - **Prune** learned names whose player has left, so a stale dynamic
          callsign cannot mis-match a later garbled call. Mission callsigns are
          never pruned.
        - **Seed** the speaker map directly ("Caveman -> Chevy 1") instead of
          waiting to learn it from what the pilot says. Because the map is keyed
          by player name, two players who share a callsign stay distinct.

        A player whose unit name carries no callsign stays unseeded and is bound
        from what they actually say (`remember_speaker`), keyed by their SRS
        name — but only if they have no binding yet this spawn. Leaving the
        server, or respawning into a different DCS unit, clears the binding so
        the new slot (or a fresh radio check-in) can set it again. STT cannot
        remap a live seat to another flight.
        """
        live_players: dict[str, str] = {}
        discovered: dict[str, str] = {}
        for player, unit_name in pairs:
            if not player:
                continue
            live_players[player] = unit_name or ""
            callsign = slot_callsign(unit_name or "")
            if callsign:
                discovered[player] = callsign
        live = list(discovered.values())
        self.callsigns.add(live)
        self.callsigns.prune(live)
        for player, unit_name in live_players.items():
            prev = self._online_players.get(player)
            if prev is not None and prev != unit_name:
                self._forget_speaker(player)
        for player in list(self._online_players):
            if player not in live_players:
                self._forget_speaker(player)
        self._online_players = dict(live_players)
        self._seeded_keys = {}
        for player, callsign in discovered.items():
            if self.callsign_for_speaker(player) == player:
                for key in self._speaker_keys(player):
                    self.speaker_callsigns[key] = callsign
                    self._seeded_keys[key] = callsign

    def known_callsigns(self, players: list[str]) -> list[str]:
        """The real callsigns of these online pilots, unknown ones omitted.

        A pilot is *known* once the speaker map resolves them to something other
        than their own name — either a slot-shaped unit name (see
        `sync_speakers`) or a callsign learned from their own transmission. Used
        as the candidate set for attributing a garbled call, so a raw unit name
        ("Kutaisi_F/A-18C Lot 20_2- 1") can never be compared against.
        """
        out: list[str] = []
        for who in players:
            callsign = self.callsign_for_speaker(who)
            if callsign and callsign != who and callsign not in out:
                out.append(callsign)
        return out

    @staticmethod
    def _norm_speaker(name: str) -> str:
        """Normalise a speaker name for lookup: lower-case, no separators.

        Digits are **significant** (see `state_client.norm_identity`): DCS
        appends a number to make duplicate names unique, so "caveman" and
        "caveman1" must NOT be conflated. A trailing-digit variant is matched
        explicitly in `_speaker_keys`, where it is safe.
        """
        return re.sub(r"[\s_-]+", "", (name or "").lower())

    @classmethod
    def _speaker_keys(cls, name: str) -> list[str]:
        """Every key a speaker name may be stored under.

        Includes the exact normalised name, plus — only when the raw name has no
        separator and no pre-existing digit — the trailing-"1" form (mirroring
        `CallsignRegistry.speaker_spellings` for a flight-name SRS name like
        "Springfield" vs "Springfield1"). This never conflates two distinct
        DCS-uniquified names ("Caveman" vs "Caveman-1"), because separators and
        digits in the raw name suppress the variant.
        """
        keys = [cls._norm_speaker(name)]
        if re.fullmatch(r"[A-Za-z]+", name or ""):
            keys.append(cls._norm_speaker(name) + "1")
        return keys

    def _forget_speaker(self, who: str) -> None:
        """Drop every speaker-map key for this player (end of a DCS spawn)."""
        keys = set(self._speaker_keys(who))
        keys.update(self.callsigns.speaker_spellings(who))
        for key in keys:
            self.speaker_callsigns.pop(key, None)
            self._seeded_keys.pop(key, None)

    @staticmethod
    def _same_aircraft(a: str, b: str) -> bool:
        """True if two callsign strings name the same seat (lead 1-1 ≡ 1)."""
        if not a or not b:
            return False
        if a.strip().lower() == b.strip().lower():
            return True

        def parts(cs: str) -> tuple[str, str, str | None] | None:
            match = re.match(r"^(.+?)\s+(\d+)(?:-(\d+))?$", cs.strip())
            if not match:
                return None
            return match.group(1), match.group(2), match.group(3)

        left, right = parts(a), parts(b)
        if not left or not right:
            return False
        return (canonical_callsign(*left).lower()
                == canonical_callsign(*right).lower())

    def callsign_for_speaker(self, who: str) -> str:
        """Flight callsign for an SRS/DCS player name, or the name itself.

        The match is case- and separator-insensitive ("Caveman" == "caveman"),
        and a flight-name speaker ("Springfield") also matches the numbered
        form DCS sometimes reports ("Springfield1"). Digits are otherwise
        significant, so two DCS-uniquified players ("Caveman" vs "Caveman-1")
        stay distinct.
        """
        if not who:
            return who
        for key in self._speaker_keys(who):
            if key in self.speaker_callsigns:
                return self.speaker_callsigns[key]
        return who

    def stt_flight_names(self) -> list[str]:
        """Flight names to bias STT toward for the next transcription.

        Once DCS (or a prior radio call) has named the pilots currently online,
        only *their* names and known errors go into Whisper's hotword list —
        a 60-flight mission dictionary would be truncated and would pull the
        decoder toward the wrong word. Falls back to the full registry when
        nobody is identified yet (first contact).
        """
        names: list[str] = []
        for callsign in self.active_pilots:
            flight = flight_name(callsign)
            if flight and flight not in names:
                names.append(flight)
        return names or list(self.callsigns.names)

    def extract_callsign(self, text: str, speaker: str = "") -> str | None:
        """The callsign a transmission is *from*, or None.

        Prefers the speaker's own known callsign when the text names it, so a
        flight name that merely appears in the message (a readback "descend to
        Enfield 15" = heading 150 to Enfield) does not hijack the transmission;
        otherwise the first callsign named. A speaker never established still
        uses the first match, and a call with no callsign returns None (the
        caller may then fall back to `_resolve_speaker`).

        When we already know this speaker (DCS seed or a prior call), matching
        is restricted to that one flight — including its known STT errors —
        so "forward one" can resolve to Ford 1 without "for the one" becoming
        Uzi 1. A focused match that names a *different* aircraft (STT "over to"
        becoming Adder 2) is ignored: the transmission is still from this seat.
        """
        own = self.callsign_for_speaker(speaker) if speaker else ""
        if own and own != speaker:
            found = self.callsigns.extract(text, focus=[own])
            if found and not self._same_aircraft(own, found):
                return own
            return found or own
        if len(self.active_pilots) == 1:
            return self.callsigns.extract(text, focus=self.active_pilots)
        found = self.callsigns.extract_all(text)
        if not found:
            return None
        return found[0]

    def _is_flight_checkin(self, text: str, speaker: str = "") -> bool:
        """True for a flight check-in: a bare callsign (or element number).

        Master Arms SOP: on switching frequency a flight checks in *as a flight*
        before addressing the agency — the lead says just the flight callsign
        ("Adder1"), each wingman answers with their number ("2"). It is a
        flight-internal call, not a call to the agency, so the controller does
        not answer it (answering it would make the flight check-in act as the
        agency contact).

        Only a transmission that is *nothing but* the callsign (optionally with
        the speaker's own name) counts, so a real request ("Dodge 1, requesting
        taxi") or an agency check-in ("Ground, Dodge 1") is never swallowed. A
        bare element number ("2", "3", "4" … for a 2/3/4-ship) is also accepted,
        in digits or words ("two"): it is never a valid call to an agency, so
        treating it as a flight check-in is safe.
        """
        rest = self.callsigns.strip(text)
        if speaker:
            # Drop the speaker's own name only when it is not an agency word: a
            # player whose SRS name is literally "Ground" must not have the
            # agency stripped out of "Ground, Dodge 1" (that is the agency
            # check-in, not a flight check-in).
            if speaker.lower() not in self._agency_words():
                rest = re.sub(rf"\b{re.escape(speaker)}\b", " ", rest,
                              flags=re.IGNORECASE)
        rest = re.sub(r"[^a-z0-9]+", "", rest.lower())
        return not rest or bool(re.fullmatch(r"[2-9]|two|three|four|five|six|"
                                             r"seven|eight|niner|nine", rest))

    def _agency_words(self) -> set[str]:
        """Lower-case agency names that address a controller, not a flight.

        Used so a pilot's own name is never mistaken for (or stripped as) an
        agency call: "Ground, Dodge 1" is the agency check-in even if the
        speaker happens to be called "Ground".
        """
        return {w.lower() for w in (self.tower_short, self.ground_short,
                                    self.control_short, self.stingray_short,
                                    "atis", "tower",
                                    "ground", "control", "approach",
                                    "departure", "center", "centre",
                                    "stingray", "awacs")}

    def _is_bare_checkin(self, text: str, speaker: str = "") -> bool:
        """True if the transmission is *only* an agency check-in.

        "Ground, Colt 1" / "Tower, Colt 1" / "Kutaisi Tower, Colt 1" name the
        agency and nothing else, so they are acknowledged ("Colt 1, Tower.").
        A call that carries a request ("Tower, Colt 1, ready for departure")
        is NOT bare — the request handler must have it.

        Without this, Tower had no acknowledgement at all and answered a bare
        check-in with "say again" (debrief 2026-10-22: every "Tower, Epic 4"
        was rejected, which left the pilot with no way to establish contact).
        """
        if not text or not self.callsigns.strip(text).strip():
            return False
        content = self.callsigns.strip(text)
        if speaker and speaker.lower() not in self._agency_words():
            content = re.sub(rf"\b{re.escape(speaker)}\b", " ", content,
                             flags=re.IGNORECASE)
        rest = _CALL_FILLER_RE.sub(" ", content.lower())
        if not rest.strip():
            return True  # "Tower, Colt 1" — the agency and nothing else
        words = set(rest.split())
        # The full agency name ("Kutaisi Tower") and the airfield name are part
        # of the address, not a request, so they count as filler too.
        allowed = self._agency_words() | {self.tower.lower(),
                                          self.ground.lower(),
                                          self.control.lower()}
        if self.airfield is not None:
            allowed.add(self.airfield.name.lower())
        allowed = {w for name in allowed for w in str(name).split()}
        return bool(words) and words <= allowed

    def _resolve_speaker(self, text: str, who: str) -> str | None:
        """Attribute a transmission with no extracted callsign to a pilot.

        Heuristic for training: if the transmission *resembles* a known pilot's
        callsign, treat it as that callsign — so a garbled "Spring fail 1-1"
        from the one pilot online is understood as "Springfield 1-1". Returns a
        callsign only when it is unambiguous, and **only when the text actually
        looks like that flight name**:

        - the speaker's own known callsign, if they earlier used it and this
          transmission still resembles it; or
        - in solo training, the *only* pilot online whose callsign it resembles.

        A transmission that does not reference any callsign (e.g. a readback
        that opens "Cleared taxi …" or "After departure …") is deliberately
        **not** attributed: those open with the content, and guessing there
        would risk acting on the wrong intent.
        """
        if not who:
            return None
        # 1) A pilot we have already identified, whose own callsign the text
        #    still resembles (a garbled repeat of their callsign).
        own = self.callsign_for_speaker(who)
        if own != who and self.callsigns.looks_like(text, own):
            return own
        # 2) Solo training: exactly one pilot online whose flight name it
        #    resembles.
        if not self.solo_mode:
            return None
        candidates = [c for c in self.active_pilots
                      if self.callsigns.looks_like(text, c)]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _note_formation(self, pilot: PilotState, low: str) -> None:
        """Learn the flight size from a position/formation call.

        A pilot says e.g. "two-ship Hornets" or "4-ship Adder11" when checking
        in; we remember it so controllers can address the flight correctly
        ("2-ship Adder11, cleared for the overhead break"). Only updates when a
        formation is actually stated, so it is not clobbered by later calls.
        """
        words = {"one": 1, "single": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6}
        m = re.search(r"\b(\d{1,2})[\s-]*ship\b", low)
        if m:
            pilot.formation = int(m.group(1))
            return
        m = re.search(r"\b(" + "|".join(words) + r")[\s-]*ship\b", low)
        if m:
            pilot.formation = words[m.group(1)]

    def _formation_prefix(self, pilot: PilotState) -> str:
        """A callsign prefix naming the flight size, or '' for a single ship."""
        n = pilot.formation
        return f"{n}-ship " if n >= 2 else ""

    def _flight_members(self, callsign: str) -> list[str]:
        """Every known pilot whose callsign belongs to the same flight.

        "Adder 1" and "Adder 2" (or "Adder11"/"Adder12") share the flight name
        "Adder" (`flight_name`). Used for the SOP rule that a lead's landing
        clearance covers the wingmen: they do not need individual clearances.
        Only *known* pilots count (someone who has actually transmitted), so a
        quiet wingman is not cleared by proxy before they ever call.
        """
        flight = flight_name(callsign)
        if not flight:
            return [callsign]
        return [cs for cs in self.pilots
                if flight_name(cs) == flight and cs != callsign]

    def _pilot(self, callsign: str) -> PilotState:
        state = self.pilots.get(callsign)
        if state is None:
            state = PilotState(callsign=callsign)
            self.pilots[callsign] = state
        return state

    def set_runway(self, runway: str) -> None:
        """Update the active runway (e.g. when the wind shifts)."""
        if runway and runway != self.runway:
            self.runway = runway

    def set_wind(self, direction: float, speed: float) -> None:
        """Update the spoken wind (MA SOP: "270, 2 knots"; calm below ~1 kt).

        `speed` is DCS metres per second; the spoken figure is knots.
        """
        self.wind_dir = float(direction or 0.0)
        self.wind_speed = float(speed or 0.0)
        if self.wind_speed < 0.5:
            self.wind = "calm"
        else:
            self.wind = f"{direction:03.0f}, {wind_speed_kt(self.wind_speed)} knots"

    def set_qnh(self, qnh_inhg: float) -> None:
        """Update the spoken QNH (Master Arms read it as 4 digits, e.g. 2992)."""
        if qnh_inhg:
            self.qnh = f"{round(qnh_inhg * 100):04d}"

    def set_weather(self, summary: str) -> None:
        """Update the weather phrase ("CAVOK" / visibility + clouds)."""
        if summary:
            self.weather = summary

    def set_bullseye(self, lat: float, lon: float) -> None:
        """Set the coalition bullseye (main reference point) from the mission.

        The pilot reads bullseye off their HSI, so this must be the mission's
        own point — a made-up one could not be verified. Until it is set, a
        bullseye call is acknowledged without a position check.
        """
        self.bullseye = (float(lat), float(lon))

    def _say(self, key: str, callsign: str, agency: str | None = None,
             **fields: object) -> str:
        # Radio preset channels are per-airfield (airspace.json); fall back to
        # the global phraseology variables when the airfield does not define
        # them, so a bare brain (tests, no airfield) still renders. Only pass
        # the channels the airfield actually defines, so a missing one does not
        # override the phraseology default with None.
        channels = getattr(self.airfield, "channels", None) or {}
        overrides = {f"{role}_channel": channels[role]
                     for role in ("ground", "tower") if role in channels}
        if "control" in channels:
            overrides["channel"] = channels["control"]
        if "stingray" in channels:
            overrides["stingray_channel"] = channels["stingray"]
        # The overhead-break direction is per-airfield, per-runway (MA: at
        # Kutaisi the break is on the south side — left for 25, right for 07).
        # Derive it from the airfield so the clearance follows the active runway.
        if self.airfield is not None:
            overrides["downwind"] = self.airfield.break_direction(self.runway)
        return self.phraseology.render(
            key, callsign=callsign, tower=self.tower_short,
            agency=agency or self.tower_short,
            runway=self.runway, wind=self.wind, ground=self.ground_short,
            control=self.control_short, stingray=self.stingray_short,
            qnh=self.qnh, weather=self.weather,
            **overrides, **fields)

    def _agency(self, controller: Controller) -> str:
        """Short name of the agency handling a controller role."""
        return {Controller.GROUND: self.ground_short,
                Controller.CONTROL: self.control_short,
                Controller.STINGRAY: self.stingray_short,
                Controller.TOWER: self.tower_short}.get(controller,
                                                         self.tower_short)

    def _challenge(self, callsign: str, agency: str,
                   track: AircraftTrack) -> str:
        """A realistic 'I don't show you there' challenge for a bad position
        report. Uses the live position so the pilot learns to report correctly.

        The challenge does NOT advance the pilot's state, so it must always
        carry a way out — otherwise a pilot whose report is (rightly or wrongly)
        rejected would be stuck. We append the escape reminder.
        """
        # Very close to the field reads better as "on the ground" than
        # "0 miles north".
        position = ("on the ground" if track.distance_nm < 1.0
                    else track.relative)
        return self._say("position_challenge", callsign, agency=agency,
                         position=position)

    def _holding_point(self, track: AircraftTrack | None) -> str:
        """Name of the holding position nearest the aircraft, or ''."""
        if track is None or self.airfield is None:
            return ""
        return self.airfield.nearest_holding_point(track.lat, track.lon) or ""

    # ---------- readback training ----------
    # A trainer that never blocks: a readback is checked against the clearance
    # actually issued. A complete readback is confirmed; an incomplete one gets
    # one targeted "say again your X" (teaching) and the phase still advances, so
    # a fumbled readback never traps the pilot — they keep flying.

    # Word equivalents for the digits, so a spoken readback ("two five",
    # "one five hundred") can be compared with the numeric clearance. Shares the
    # tables with the post-STT folder so "angels fifteen" and "angels one five"
    # both compare equal to a "15" clearance.
    _WORD_DIGITS = _NUMBER_WORDS

    # A run of number words is one spoken figure ("two niner niner two" ->
    # 2992, "thirty two" -> 32, "one five zero zero" -> 1500), with an optional
    # trailing "thousand" ("two one thousand" -> 21000).
    _NUMBER_RUN_RE = re.compile(
        r"\b(?:" + _FIGURE_WORD + r")(?:\s+(?:" + _FIGURE_WORD + r"))*"
        r"(?:\s+thousand)?\b")

    @classmethod
    def _numbers(cls, text: str) -> set[str]:
        """Every number in `text`, with spoken digits folded in.

        "two niner niner two" -> {2992}; "0 7 0" -> {070}; "1500 feet" -> {1500};
        "angels fifteen" -> {15}; "angels thirty two" -> {32}. Used to compare a
        readback against the clearance without depending on the filler words or
        the exact phrasing.
        """
        t = re.sub(r"[.,]", "", (text or "").lower())
        t = cls._NUMBER_RUN_RE.sub(
            lambda m: _spoken_figure(m.group(0)) or m.group(0), t)
        t = re.sub(r"(?<=\d)\s+(?=\d)", "", t)  # "2 9 9 2" -> "2992"
        return set(re.findall(r"\d+", t))

    @staticmethod
    def _number_read_back(number: str, spoken: set[str]) -> bool:
        """True if `number` was read back, tolerating STT gluing the callsign on.

        STT often runs the callsign's digits into the last clearance number
        ("...hold short runway zero seven" + "Ford one" -> "0741"), so an exact
        set match would wrongly report the runway as missing. Accept a spoken
        number that *starts with* the clearance number.

        Leading zeros are also tolerated: STT writes the spoken word ("seven"
        -> "7") while the clearance says "07", so compare with zeros stripped.
        """
        bare = number.lstrip("0") or "0"
        for s in spoken:
            s_bare = s.lstrip("0") or "0"
            if s == number or s_bare == bare:
                return True
            if len(s) > len(number) and s.startswith(number):
                return True
        return False

    def _readback_item(self, low: str, clearance: str) -> str | None:
        """The label of the first item the pilot failed to read back, or None.

        Only the clearance's numbers (runway/QNH/altitude/heading) and gate names
        are required — the filler wording is ignored, so a normal abbreviated
        readback passes. Callsigns are stripped from both sides first: the
        clearance text contains the callsign ("Pontiac 1, Ground, ..."), and its
        flight number must not be compared as a clearance value (a readback
        ending "Pontiac 11" would otherwise look like a missing "1").
        """
        clearance = self.callsigns.strip(clearance or "")
        low = self.callsigns.strip(low or "")
        spoken = self._numbers(low)
        missing_num = [n for n in self._numbers(clearance)
                       if not self._number_read_back(n, spoken)]
        # Only a gate named as "Exit X" / "Entry X" counts. A bare gate word
        # collides with parking-area names ("Ramp South" contains "South"), so
        # matching the word alone would demand a gate the clearance never gave.
        gates = [g for g in self.gates
                 if re.search(rf"\b(?:exit|entry)\s+{re.escape(g)}\b",
                              clearance, re.IGNORECASE)]
        missing_gate = [g for g in gates if g.lower() not in low]
        if not missing_num and not missing_gate:
            return None
        # A gate WAS named, just a different one ("after departure, turn right,
        # exit north" when Exit East was cleared): the pilot did read back an
        # exit — saying "I did not get your exit" is confusing. The honest
        # controller response is to say the clearance again, so label it as a
        # full "say again" of the clearance instead of a missing item.
        if missing_gate and any(re.search(rf"\b(?:exit|entry)\s+{re.escape(g)}\b",
                                          low, re.IGNORECASE) for g in self.gates):
            return "clearance"
        return self._item_label(clearance, missing_num, missing_gate)

    def _completes_clearance(self, low: str, pilot: PilotState) -> bool:
        """True if a short follow-up ("runway 07") supplies the item a prior
        readback was missing. Mirrors _readback_item's matching: numbers must
        match a clearance number (leading zeros tolerated), gates must be named
        with Exit/Entry.
        """
        clearance = self.callsigns.strip(pilot.last_clearance
                                         or pilot.last_reply)
        if not clearance:
            return False
        spoken = self._numbers(low)
        if all(self._number_read_back(n, spoken)
               for n in self._numbers(clearance)):
            return True
        for g in self.gates:
            if re.search(rf"\b(?:exit|entry)\s+{re.escape(g)}\b",
                         clearance, re.IGNORECASE) \
                    and re.search(rf"\b(?:exit|entry)\s+{re.escape(g)}\b",
                                  low, re.IGNORECASE):
                return True
        return False

    def _check_readback(self, callsign, pilot, low, clearance, controller,
                        ok_key=None):
        """Confirm a readback, or flag it incomplete — but never block.

        A **correct** readback is acknowledged only when the controller has
        nothing else to say (real/MA practice): if a next instruction follows,
        the controller just gives it, so the default is **silent** (""). Pass
        `ok_key` to force an explicit acknowledgement where nothing follows
        (e.g. the Ground runway/QNH readback).

        One exception, and only one: if an *earlier* attempt at this clearance
        was incomplete (`readback_fails > 0`), the corrected readback IS
        confirmed with "readback correct". Saying nothing after a "readback
        incomplete" leaves the pilot unsure whether the fix landed (debrief
        2026-10-22: "ATC bör bekräfta att readbacken sedan har blivit korrekt").
        A clean first readback keeps the silent behaviour, so every tested
        kneeboard dialog is unchanged.

        An incomplete readback is **not** called correct: it gets an honest
        "readback incomplete — {item} not read back, continue." so the pilot
        knows what to train on, and the caller still advances the phase, so a
        fumbled readback never traps them — they keep flying and fix it next
        time.
        """
        agency = self._agency(controller)
        item = self._readback_item(low, clearance)
        pilot.last_readback = True
        if item is None:
            corrected = pilot.readback_fails > 0
            pilot.readback_fails = 0
            if ok_key or corrected:
                return self._say(ok_key or "readback_correct", callsign,
                                 agency=agency)
            return ""
        pilot.readback_fails += 1
        if item == "clearance":
            # A wrong item was read back (e.g. a different exit): the honest
            # response is the clearance again, not "I did not get your exit".
            return self._say("say_again", callsign, agency=agency)
        return self._say("readback_incomplete", callsign, agency=agency,
                         item=item)

    def _readback_coaching(self, callsign, pilot, low, clearance,
                           controller) -> str:
        """A readback-incomplete note for a clearance that is issued regardless.

        For the takeoff clearance (always given), so the pilot still gets the
        "incomplete — continue" feedback without the clearance being withheld.
        """
        pilot.last_readback = True
        item = self._readback_item(low, clearance)
        if item is None:
            pilot.readback_fails = 0
            return ""
        pilot.readback_fails += 1
        if item == "clearance":
            # Wrong-item readback: the clearance itself follows right away, a
            # "say again" here would be noise.
            return ""
        return self._say("readback_incomplete", callsign,
                         agency=self._agency(controller), item=item) + " "

    @staticmethod
    def _item_label(clearance: str, missing_num, missing_gate) -> str:
        """A short label for the missing readback item ("say again your X")."""
        low = clearance.lower()
        if missing_gate:
            return "exit" if "exit" in low else "entry"
        if "qnh" in low or "q.n.h" in low:
            return "QNH"
        if "heading" in low:
            return "heading"
        if ("angels" in low or "climb" in low or "descend" in low
                or "ft" in low or "feet" in low or "below" in low):
            return "altitude"
        if "runway" in low:
            return "runway"
        if "hold short" in low:
            return "hold short"
        if "taxi" in low:
            return "taxi clearance"
        return "readback"

    def _runway_busy(self, traffic: list | None, exclude: str = "") -> bool:
        """True if the active runway is occupied by other traffic (players + AI).

        Used to sequence clearances: don't line up / clear to land onto an
        occupied runway. Without live traffic we trust the pilot (offline).

        `exclude` is the transmitting pilot's SRS/DCS name. The live units are
        keyed by DCS unit name, while the brain works in flight callsigns, so we
        exclude by the *player* name (falling back to the unit name) — otherwise
        the pilot counts as occupying the runway themselves and is wrongly told
        to hold short.
        """
        if not traffic or self.airfield is None:
            return False
        return self.airfield.runway_occupied(traffic, exclude=exclude or None)

    def handle(self, text: str, track: AircraftTrack | None = None,
               controller: Controller = Controller.TOWER,
               traffic: list | None = None,
               speaker: str = "") -> str | None:
        """Return the ATC reply for a pilot transmission, or None if we
        did not understand it (no callsign, unknown request).

        `track` is the aircraft's live position (if known), used to make
        inbound replies distance-aware. `controller` is which agency the
        transmission was addressed to (from the frequency it arrived on).
        `traffic` is every live unit (players + AI), used to sequence clearances
        against other traffic (e.g. hold short if the runway is occupied).
        `speaker` is the transmitting pilot's SRS/DCS name, used to exclude them
        from the runway-occupancy check (they are not traffic to themselves).
        """
        # Flight check-in (Master Arms SOP): on switching frequency a flight
        # checks in *as a flight* before addressing the agency — the lead says
        # just the flight callsign ("Adder1"), each wingman answers with their
        # number ("2"). It is a flight-internal call, not a call to the agency,
        # so the controller stays silent. Checked before callsign extraction so
        # the wingman's bare number is caught too (it names no callsign).
        if self._is_flight_checkin(text, speaker):
            return ""

        # Every callsign the text names, in order. A transmission can mention
        # several (a readback "descend to Enfield 15" names Enfield; a formation
        # call names two flights). Prefer the *speaker's own* callsign when it is
        # among them, so a content flight name does not hijack the transmission
        # (this was "Climbing to Enfield 15" being handled as flight Enfield).
        callsign = self.extract_callsign(text, speaker)
        if not callsign:
            # Some calls may omit the flight number (e.g. "Colt help").
            name = self.callsigns.extract_name(text)
            if name and re.search(r"\btrainer\b", text.lower()) and re.search(
                    r"\b(help|assist|what do i do|what now|remind me)\b",
                    text.lower()):
                return self._help(name, self._pilot(name), controller, track)
            # A speaker we have already identified is trusted: use their own
            # callsign even when this transmission did not name it. STT often
            # drops or mangles the callsign, and we already know who is on the
            # radio (SRS player name -> callsign), so a good experience beats
            # testing the STT. Falls back to the resemblance heuristic.
            own = self.callsign_for_speaker(speaker) if speaker else ""
            if own and own != speaker:
                callsign = own
            else:
                callsign = self._resolve_speaker(text, speaker)
        if not callsign:
            # Solo training: a transmission we cannot tie to a callsign gets a
            # spoken "say again" prompt, so the pilot knows they were heard but
            # not understood (most often a forgotten callsign). On a multi-pilot
            # server we stay silent instead, to avoid replying to traffic meant
            # for someone else.
            #
            # Only prompt a pilot we can place. With nobody known to be online
            # there is no callsign to learn — a dynamic slot has none, and STT
            # cannot render one we never knew — so the prompt would repeat
            # forever and the pilot could never satisfy it. Stay silent until
            # they sign in successfully; then the prompt works as intended.
            if self.solo_mode and self.active_pilots:
                return self._say("say_again_no_callsign", "",
                                 agency=self._agency(controller))
            return None
        low = text.lower()
        pilot = self._pilot(callsign)
        # One-shot state of the PREVIOUS transmission: the pending coach note
        # belongs to exactly one exchange, and the readback flag only truthful
        # for the call being judged. A caller that never took the note (headless
        # test, worker crash) must not be taught a stale clearance.
        pilot.pending_coach = ""
        pilot.last_readback = False
        previous_controller = pilot.checked_in_at or pilot.last_controller
        if previous_controller and previous_controller != controller.value:
            # A received check-in completes a transfer, even if the pilot
            # missed the push or changed frequency without one. Do not advance
            # the phase when ATC merely issues a handoff.
            self._enter_agency(pilot, controller)
            if (controller == Controller.TOWER
                    and previous_controller == Controller.CONTROL.value
                    and pilot.phase == Phase.INBOUND):
                pilot.phase = Phase.LANDING
            if (pilot.pending_handoff_to
                    and pilot.pending_handoff_to != controller.value):
                # A direct check-in on another agency supersedes an old push.
                pilot.pending_handoff_to = ""
        pilot.last_controller = controller.value
        pilot.checked_in_at = controller.value
        if pilot.pending_handoff_to == controller.value:
            pilot.pending_handoff_to = ""
        if pilot.requested_handoff_to == controller.value:
            pilot.requested_handoff_to = ""
        self._note_formation(pilot, low)
        # STT normalisation, applied once, at the point where every intent is
        # matched against the text: see `_fold_spoken_figures`. Done *after*
        # formation/check-in handling above, which need the word forms.
        # Frequent STT rendering of "marking bulls" from the sortie log.
        low = re.sub(r"\bmocking\s+(bulls?|bullseye)\b", r"marking \1", low)
        low = _fold_spoken_figures(low)
        low = _fold_bullseye_figures(low)
        reply = self._dispatch(callsign, pilot, low, track, controller, traffic,
                               speaker)
        if reply:
            handoff = re.search(
                r"\bcontact\s+(?:(?:kutaisi|[\w-]+)\s+)?"
                r"(ground|tower|control|stingray)\b", reply, re.I)
            if handoff:
                pilot.pending_handoff_to = handoff.group(1).lower()
        if reply and not self._is_meta_reply(reply):
            # Remember the last clearance so the pilot can ask for it again
            # ("say again") — a trainer aid for missed readbacks. Meta replies
            # ("say again", "readback incomplete") are NOT stored: storing one
            # made a later "repeat" replay the "say again" itself, an endless
            # loop that hid the real clearance (debrief 2026-10-18 12:44:32).
            pilot.last_reply = reply
        # Coach step (AFTER last_reply is stored, so a coached clearance is
        # replayed clean by "say again" — the coach note is not part of the
        # clearance). Coach mode ONLY: casual mode never sets a note, so its
        # behaviour is byte-identical to the pre-coach bot.
        #
        # The note is NOT spoken on the radio: it would pollute the frequency
        # for everyone on the server. It is stored as `pending_coach` on the
        # pilot and the caller (controller worker) delivers it as a direct
        # in-game message to THAT pilot — the real ATC voice stays clean, and
        # a call that was RESOLVED fine (e.g. a readback that opened with the
        # callsign) still gets its teaching note in the pilot's feed.
        # The coach is a post-step on the utterance text: intent recognition
        # is untouched, and a note is never set on semantic feedback
        # ("readback incomplete" already corrected this transmission) or meta
        # chatter (trainer commands).
        if self.mode == "coach" \
                and not ("trainer" in reply.lower() if reply else False) \
                and "readback incomplete" not in (reply or "").lower():
            proper = coach.note(low)
            if not proper and pilot.last_readback:
                proper = coach.note_readback(low, callsign)
            if proper:
                pilot.pending_coach = self._say("coach_wording", callsign,
                                                proper=proper)
        return reply

    def _is_meta_reply(self, reply: str) -> bool:
        """True for housekeeping replies that must not shadow the clearance.

        "say again" and "readback incomplete" are feedback about a readback,
        not clearances. If they replaced `last_reply`, a follow-up "repeat"
        would replay the feedback instead of the clearance the pilot asked
        for. `last_clearance` (set when a clearance is issued) is unaffected.
        """
        return ("say again" in reply.lower()
                or "readback incomplete" in reply.lower())

    def remember_transmission(self, callsign: str, text: str) -> None:
        """Remember the latest ATC message addressed to a pilot for replay."""
        if callsign and text and not self._is_meta_reply(text):
            self._pilot(callsign).last_reply = text
    def _dispatch(self, callsign: str, pilot: PilotState, low: str,
                  track: AircraftTrack | None,
                  controller: Controller,
                  traffic: list | None = None,
                  speaker: str = "") -> str | None:
        """Route one transmission to the right intent handler."""
        # Trainer commands: "trainer, help" and "trainer, echo on|off" (or
        # "trainer, <callsign>, ..."). A namespaced trainer aid (NOT real
        # phraseology), available on any frequency and in any phase. Handled
        # before everything else so it can never be mistaken for a clearance.
        if re.search(r"\btrainer\b", low):
            return self._trainer(callsign, pilot, low, controller, track)

        # Authentication (RAMROD) is deliberately not implemented: it needs a
        # prebriefed key, which is a mission-design decision, not a trainer
        # feature. A pilot who tries it must not be left with a bare "say
        # again" — that reads as "you said it wrong" and invites them to keep
        # retrying a thing that can never work. Answer in ATC style and give
        # them a way forward. Handled before the controller dispatch so it
        # works on every frequency.
        if re.search(r"\b(auth|authenticat\w*|ramrod)\b", low):
            return self._say("authentication_not_implemented", callsign,
                             agency=self._agency(controller))

        # Emergency (MAYDAY / PAN-PAN / "declaring an emergency") is not
        # simulated, but it must NEVER be answered with a bare "say again" —
        # that reads as "you said it wrong" to a pilot who just declared an
        # emergency. Acknowledge it in ATC style and give them a way forward.
        # Handled before the controller dispatch so it works on every
        # frequency, and before the capability branch below (which would
        # otherwise match "declaring").
        if re.search(r"\b(mayday|pan[\s-]?pan|declaring an emergency|"
                     r"declaring emergency|emergency)\b", low):
            return self._say("emergency_not_implemented", callsign,
                             agency=self._agency(controller))

        # Brevity calls that are C2/GCI (AWACS) functions — PICTURE, DECLARE,
        # BOGEY DOPE, SNAP — are not simulated. Answer honestly in ATC style
        # rather than with a false ack or a bare "say again", so the pilot
        # knows the trainer does not model them and can carry on. Handled
        # before the controller dispatch so it works on every frequency.
        capability = re.search(r"\b(picture|declare|bogey dope|snap)\b", low)
        if capability and not re.search(
                r"\b(bullseye|bull'?s?eye|marking)\b", low):
            return self._say("capability_not_implemented", callsign,
                             agency=self._agency(controller),
                             capability=capability.group(1).upper())

        # Trainer aid: "<callsign> help" (also "assist", "what do I do", "what
        # now", "remind me") gives a state-aware hint. Accepted *without* the
        # "trainer" prefix: a stuck pilot says "Control, Epic 4, help", and
        # demanding a magic word made the aid unreachable exactly when it was
        # needed (debrief 2026-10-22: four help calls, all swallowed as
        # check-ins or "say again"). It must come *before* the controller
        # dispatch, because a bare "Control ..." is otherwise answered as a
        # frequency check-in and the real question is lost.
        if re.search(r"\b(help|assist|what do i do|what now|remind me)\b", low):
            return self._help(callsign, pilot, controller, track)

        if re.search(r"\b(speed restriction|speed limit|restricted speed|"
                     r"maximum speed|any speed|how fast)\b", low):
            agency = self._agency(controller)
            if track is None or self.airfield is None:
                return self._say("speed_position_unknown", callsign,
                                 agency=agency)
            inside_ctr = self._inside_ctr(track)
            if inside_ctr:
                speed = self.airfield.ctr_speed_limit_kt
                if speed is not None:
                    return self._say("speed_restriction_ctr", callsign,
                                     agency=agency, speed=f"{speed:g}")
                return self._say("speed_restriction_none", callsign,
                                 agency=agency)
            return self._say("speed_restriction_outside", callsign,
                             agency=agency)

        # Consume a readback of the assigned vector before handling requests.
        # Without this, "281, vectors, runway 07" was mistaken for a new
        # request and got a different heading as the aircraft moved.
        if re.search(r"\bvectors?\b", low):
            request_vector = bool(re.search(
                r"\b(request(?:ing)?|need|want|like|would like|asking for|"
                r"give me)\b", low))
            if not request_vector:
                spoken = self._numbers(low)
                runway_readback = (
                    not pilot.vector_runway
                    or self._number_read_back(pilot.vector_runway, spoken))
                if (pilot.vector_heading
                        and self._number_read_back(pilot.vector_heading, spoken)
                        and runway_readback):
                    return ""
                return self._say("say_again", callsign,
                                 agency=self._agency(controller))

        # Trainer aid: "<callsign> request vectors [for runway 25]" gives an
        # approach vector ("fly heading X, vectors for runway Y"). Real ATC
        # phraseology — distinct from a bearing/distance request. A *request*
        # for a heading ("request heading to the field") is the same aid; a
        # readback ("turn right heading 150") is not (it has no request verb).
        # A bullseye/marking call is excluded: "request vector to bullseye" is
        # a picture request, not an approach vector, and must not be answered
        # with a runway heading.
        vector_request = (
            (bool(re.search(r"\bvectors?\b", low)) and bool(re.search(
                r"\b(request(?:ing)?|need|want|like|would like|asking for|"
                r"give me)\b", low)))
            or (bool(re.search(r"\bheading\b", low)) and bool(re.search(
                r"\b(request(?:ing)?|like|prefer|would like|asking for|"
                r"need|give me|what)\b", low)))
        )
        if (vector_request
                and not re.search(r"\b(bullseye|bull'?s?eye|marking)\b", low)):
            return self._vectors(callsign, low, track, controller)

        # Trainer aid: "<callsign> request bearing and distance [to <gate>]"
        # gives a bearing/distance to a named entry/exit gate (or the field).
        # "azimuth" is a synonym for bearing.
        if re.search(r"\b(bearing|azimuth|directions?|how do i get|where is)\b",
                     low):
            return self._directions(callsign, low, track, controller, pilot)

        # Hidden TopGun easter egg (env-gated). A request to bust/buzz the tower
        # gets the film's canned denial and arms the stunt; it changes no phase.
        # Only Tower and Control handle it — a request on Ground is just "say
        # again" (you ask the tower, not the ramp), so the gag is not too easy.
        if topgun.enabled() and controller in (Controller.TOWER,
                                               Controller.CONTROL):
            if topgun.spike(low):
                self._topgun_armed.add(callsign)
                return topgun.NEGATIVE_LINE
            if callsign in self._topgun_armed and topgun.stall(low):
                self._topgun_armed.discard(callsign)
                return topgun.NEGATIVE_LINE

        # Escape hatches (any frequency). These exist so a pilot can never get
        # stuck in a wrong state during training:
        #   reset  -> back to the default (Idle) state
        #   cancel -> cancel the current clearance, step back one phase
        #   repeat -> replay the last clearance (missed readback)
        if re.search(r"\b(reset|restart|start over|new flight|reset state|"
                     r"back to start|scratch the flight)\b", low):
            return self._reset(callsign, pilot, controller)
        if re.search(r"\bnever\s*mind\b", low):
            return ""
        if re.search(r"\b(cancel|cancelling|canceling|disregard|abort|aborting|"
                     r"scratch that|scratch|belay|cancel clearance)\b", low):
            return self._cancel(callsign, pilot, controller)
        if re.search(r"\b(say again|repeat|repeat last|say again please|"
                     r"repeat clearance)\b", low):
            return pilot.last_reply or self._say(
                "say_again", callsign, agency=self._agency(controller))

        # Frequency-change *request* ("request channel change", "request
        # Stingray", "when can I push"): a real controller answers this, so the
        # trainer must too. A pilot who is told "shut up and fly your clearance"
        # at this point has no way forward, which is what "man kan fastna på en
        # kanal" describes (debrief 2026-10-22: 4 minutes of Control round
        # trips after the Stingray handoff was already given).
        if _CHANNEL_REQUEST_RE.search(low):
            return self._channel_request(callsign, pilot, controller, low,
                                         track)

        # Frequency-change acknowledgement ("Colt 1, channel 8, push"): the pilot
        # is switching, so we stay *silent* — a "roger" would only step on the
        # new frequency's first call. Spoken numbers and the "pushing/going to"
        # forms count too: STT writes "channel seven", never "channel 7", so the
        # old digit-only guard let every real readback fall through to "say
        # again" (debrief 2026-10-22).
        #
        # This must run BEFORE the controller dispatch. Ground and Control each
        # re-issue a pending handoff for any call in the handed-off phase, so a
        # "channel seven, push" acknowledgement was answered with the handoff
        # again — twice in a row in the 2026-10-22 sortie, stepping on the pilot
        # as they changed frequency. A genuine request still reaches
        # _channel_request above (it is matched first), and "say again" still
        # works, so a pilot who missed the handoff is never stranded.
        if _CHANNEL_CHANGE_RE.search(low) \
                and not re.search(r"\b(request|clearance|taxi|inbound|requesting)\b",
                                  low):
            return ""

        # Bare agency check-in ("Tower, Colt 1"): acknowledge it. Ground and
        # Control each already had a handler for this, but Tower had none at
        # all, so every bare Tower check-in was answered "say again" and the
        # pilot could not even establish contact (debrief 2026-10-22: seven
        # consecutive "Tower, Epic 4" calls, all rejected, while the pilot was
        # on final). Deliberately Tower-only: Ground and Control already answer
        # this correctly (and the Control one also arms the arrival flow), so
        # their behaviour is left byte-identical.
        if (controller == Controller.TOWER
                and _BARE_CHECKIN_RE.search(low)
                and self._is_bare_checkin(low, speaker)):
            if pilot.phase in (Phase.INBOUND, Phase.LANDING):
                return self._say("tower_report_position", callsign)
            return self._say("tower_ack", callsign, agency=self.tower_short)

        if controller == Controller.GROUND:
            reply = self._handle_ground(callsign, pilot, low, track, traffic)
        elif controller == Controller.CONTROL:
            reply = self._handle_control(callsign, pilot, low, track)
        elif controller == Controller.STINGRAY:
            reply = self._handle_stingray(callsign, pilot, low, track)
        else:
            reply = self._handle_tower(callsign, pilot, low, track, traffic,
                                       speaker)
        if reply is not None:
            return reply

        agency = self._agency(controller)

        # Readback: a pilot repeating a clearance. Master Arms requires a
        # readback of the departure clearance, taxi clearance, line-up and the
        # Control join/descent; the controller confirms with "readback correct".
        # Only expected in phases where a readback is actually due.
        readback = re.search(
            r"\b(readback|read back|copy|roger|wilco|cleared|hold short|line up|"
            r"lined up|waiting|turn|heading|descend|descent|climb|angels|exit|"
            r"via|in use|qnh|q\.?n\.?h\.?)\b",
            low)
        if readback and pilot.phase == Phase.LINEUP:
            # Readback of "line up and wait". The takeoff clearance is NOT given
            # on the readback alone: the pilot has only acknowledged the
            # instruction, they have not yet taxied onto the runway. Wait until
            # the aircraft is actually lined up (at the threshold, pointing down
            # the runway, stationary) — the monitor issues the clearance then
            # (see check_lineup). Without a live track we cannot tell, so we
            # fall back to the old behaviour and clear on the readback.
            if not self._lined_up(track):
                pilot.lineup_readback = True
                return ""
            coach = self._readback_coaching(callsign, pilot, low,
                                            pilot.last_clearance or pilot.last_reply,
                                            controller)
            pilot.phase = Phase.TAKEOFF
            return coach + self._takeoff_clearance(callsign, pilot)
        if readback and pilot.phase in (Phase.CLEARANCE, Phase.TAXI,
                                        Phase.INBOUND):
            return self._check_readback(callsign, pilot, low,
                                        pilot.last_clearance or pilot.last_reply,
                                        controller)

        # shared intents, valid on any frequency
        if re.search(r"\bradio check\b|\bhow (do you )?(read|copy)\b|"
                     r"\breadability\b|\bcomm check\b", low):
            return self._say("radio_check", callsign, agency=agency)
        if re.search(r"\bchecking (in|out)\b|\bwith you\b", low):
            return self._say("roger", callsign, agency=agency)
        if re.search(r"\b(reading back|roger|wilco|copy)\b", low):
            return self._say("roger", callsign, agency=agency)
        # bare callsign / unintelligible request: ask them to say again
        return self._say("say_again", callsign, agency=agency)

    def _channel_request(self, callsign: str, pilot: PilotState,
                         controller: Controller, low: str = "",
                         track: AircraftTrack | None = None) -> str:
        """Answer a pilot who asks to change frequency.

        This is a **trainer**: the pilot may ask to move to any agency from any
        agency, because we do not know what they want to practise. So the reply
        names the channel for the agency they asked for, and the pilot's phase
        is set to match where they are going — otherwise the next agency would
        treat them as if they had never left.

        When no agency is named ("request channel change"), the SOP "next" step
        for where they are is used: an airborne departure to Stingray, an
        arrival to Tower, a pilot on Ground to Tower, a pilot on Stingray back
        to Control.
        """
        target = self._requested_agency(low)
        if target is None:
            target = self._next_agency(pilot, controller)
        if target == controller:
            # Asking for the agency they are already on: just acknowledge.
            return self._say("agency_ack", callsign,
                             agency=self._agency(controller))
        pilot.requested_handoff_to = target.value
        # Tower retains the departure until it passes its exit arc. An early
        # request is acknowledged with the expected point; the normal radar
        # handoff will happen automatically when that point is reached.
        if (controller == Controller.TOWER and target == Controller.CONTROL
                and pilot.phase == Phase.TAKEOFF):
            handoff = self.check_departure_handoff(callsign, track)
            if handoff:
                return handoff
            return ""
        return self._say("contact_agency", callsign,
                         agency=self._agency(controller),
                         target=self._agency(target),
                         target_channel=self._channel_for(target))

    def _requested_agency(self, low: str) -> Controller | None:
        """The agency a pilot named in a frequency request, or None.

        "request Control" -> CONTROL, "request Stingray" -> STINGRAY. AWACS
        synonyms (magic, overlord) map to Stingray, since that is the AWACS
        position this trainer simulates.

        Only the text *after* the request verb is searched: the opening address
        names the agency the pilot is already talking to ("Tower, Colt 1,
        request Control"), and matching that would read the request as "stay
        where you are".
        """
        match = re.search(
            r"\b(?:request(?:ing)?|ask(?:ing)? for|like|need|want|would like|"
            r"can i|when can i|permission to)\b(.*)$", low)
        tail = match.group(1) if match else low
        if re.search(r"\b(stingray|awacs|magic|overlord)\b", tail):
            return Controller.STINGRAY
        if re.search(r"\b(control|centre|center)\b", tail):
            return Controller.CONTROL
        if re.search(r"\btower\b", tail):
            return Controller.TOWER
        if re.search(r"\bground\b", tail):
            return Controller.GROUND
        return None

    def _next_agency(self, pilot: PilotState,
                     controller: Controller) -> Controller:
        """The SOP "next" agency when a pilot asks to move without naming one."""
        if controller == Controller.TOWER and pilot.phase == Phase.TAKEOFF:
            return Controller.CONTROL
        if controller == Controller.CONTROL:
            if pilot.phase in (Phase.INBOUND, Phase.LANDING):
                return Controller.TOWER
            if pilot.phase == Phase.AIRBORNE:
                return Controller.STINGRAY
        if controller == Controller.STINGRAY:
            return Controller.CONTROL
        if controller == Controller.GROUND and pilot.phase in (
                Phase.HOLDING, Phase.TAXI):
            return Controller.TOWER
        if controller == Controller.TOWER and pilot.phase in (
                Phase.LANDING, Phase.TAXI):
            return Controller.GROUND
        return controller

    def _enter_agency(self, pilot: PilotState, target: Controller) -> None:
        """Set the phase to match the agency the pilot is moving to.

        Without this the new agency would treat them as if they had never left
        (e.g. a pilot who self-moves to Control would not be seen as inbound).
        """
        if target == Controller.STINGRAY:
            pilot.phase = Phase.STINGRAY
            pilot.rtb_requested = False
            pilot.rtb_control_handoff_announced = False
            pilot.rtb_control_handoff_not_before = 0.0
        elif target == Controller.CONTROL:
            # A departure moving from Tower to Control stays in the departure
            # flow. An arrival that returns to Control after a Tower check-in
            # stays in Landing, so it is sent back to Tower instead of being
            # given a fresh join. Stingray RTB and other arrivals begin inbound.
            if pilot.phase in (Phase.TAKEOFF, Phase.AIRBORNE):
                pilot.phase = Phase.AIRBORNE
            elif pilot.phase != Phase.LANDING:
                pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = False
        elif target == Controller.TOWER:
            if pilot.phase in (Phase.IDLE, Phase.CLEARANCE, Phase.TAXI):
                pilot.phase = Phase.HOLDING
            elif pilot.phase in (Phase.AIRBORNE, Phase.STINGRAY,
                                 Phase.INBOUND, Phase.LANDING):
                # An airborne pilot who self-moves to Tower is arriving, not
                # departing: without this the phase stayed Airborne/Stingray and
                # Tower did not recognise them as an arrival (the "fastnade"
                # report — a pilot who moved to Tower could not get a landing).
                pilot.phase = Phase.INBOUND
        elif target == Controller.GROUND:
            if pilot.phase in (Phase.LANDING, Phase.INBOUND):
                pilot.phase = Phase.TAXI
            elif pilot.phase in (Phase.AIRBORNE, Phase.STINGRAY):
                # Same for Ground: an airborne pilot asking for Ground wants
                # ground handling, so treat them as taxiing rather than leaving
                # them in an airborne phase Ground has no handler for.
                pilot.phase = Phase.TAXI

    def _channel_for(self, controller: Controller) -> str:
        """The preset channel number for an agency (from airspace.json)."""
        channels = getattr(self.airfield, "channels", None) or {}
        return str(channels.get(controller.value, ""))

    def _trainer(self, callsign: str, pilot: PilotState, low: str,
                 controller: Controller,
                 track: AircraftTrack | None = None) -> str:
        """Handle a "trainer, ..." command (trainer aid, any frequency).

        Currently: "trainer, echo on|off" toggles the in-game comms mirror for
        THIS pilot; "trainer, volume X" sets the GLOBAL TTS volume;
        "trainer, coach mode|casual mode" sets the GLOBAL trainer mode.
        Unknown trainer commands get a short usage hint.
        """
        agency = self._agency(controller)
        if re.search(r"\b(help|assist|what do i do|what now|remind me)\b", low):
            return self._help(callsign, pilot, controller, track)
        if re.search(r"\becho\b", low):
            if re.search(r"\b(off|stop|disable|no)\b", low):
                pilot.mirror_logs = False
                return self._say("trainer_echo_off", callsign, agency=agency)
            if re.search(r"\b(on|start|enable|yes)\b", low):
                pilot.mirror_logs = True
                return self._say("trainer_echo_on", callsign, agency=agency)
        if re.search(r"\bvolume\b", low):
            match = re.search(r"\bvolume\b\s*(?:to\s*)?(\d+(?:\.\d+)?)", low)
            if match:
                self.tx_volume = max(0.0, min(1.0, float(match.group(1))))
                return self._say("trainer_volume", callsign, agency=agency,
                                 volume=f"{self.tx_volume:.2f}")
        if re.search(r"\b(coach|teaching)\b", low):
            self.mode = "coach"
            return self._say("trainer_mode_coach", callsign, agency=agency)
        if re.search(r"\bcasual\b", low):
            self.mode = "casual"
            return self._say("trainer_mode_casual", callsign, agency=agency)
        return self._say("trainer_unknown", callsign, agency=agency)

    def mirror_enabled(self, callsign: str) -> bool:
        """True if this pilot asked for the in-game comms mirror."""
        pilot = self.pilots.get(callsign)
        return bool(pilot and pilot.mirror_logs)

    def take_coach(self, callsign: str) -> str:
        """Take and clear the pending wording-coach note (one-shot).

        Called by the controller worker after `handle`; the note is then
        delivered as a direct in-game message to that pilot. "" = nothing
        pending (casual mode, a proper call, or no such pilot).
        """
        pilot = self.pilots.get(callsign or "")
        if pilot is None:
            return ""
        note, pilot.pending_coach = pilot.pending_coach, ""
        return note

    def _reset(self, callsign: str, pilot: PilotState,
               controller: Controller) -> str:
        """Full reset to the default state (trainer escape hatch)."""
        pilot.phase = Phase.IDLE
        pilot.cleared_inbound = False
        pilot.cleared_landing = False
        pilot.break_cleared = False
        pilot.pattern_flown = False
        pilot.go_around_issued = False
        pilot.descend_issued = False
        pilot.descend_intermediate = False
        pilot.climb_issued = False
        pilot.control_checkin_received = False
        pilot.stingray_handoff_announced = False
        pilot.exit_gate = ""
        pilot.entry_gate = ""
        pilot.vector_heading = ""
        pilot.vector_runway = ""
        pilot.arrival_previous_range_nm = None
        pilot.arrival_previous_bearing_deg = None
        pilot.readback_fails = 0
        pilot.parking_cleared = False
        pilot.awaiting_takeoff = False
        pilot.awaiting_landing = False
        pilot.takeoff_ready_announced = False
        pilot.landing_ready_announced = False
        pilot.lineup_readback = False
        pilot.descend_readback = False
        pilot.landing_handoff_announced = False
        pilot.rtb_requested = False
        pilot.rtb_control_handoff_announced = False
        pilot.rtb_control_handoff_not_before = 0.0
        pilot.tower_handoff_announced = False
        pilot.pending_handoff_to = ""
        pilot.requested_handoff_to = ""
        pilot.pending_coach = ""
        return self._say("state_reset", callsign,
                         agency=self._agency(controller))

    def _cancel(self, callsign: str, pilot: PilotState,
                controller: Controller) -> str:
        """Cancel the current clearance and step back one phase.

        The ATC phrase is "cancel" (also "disregard"); "abort" is accepted as a
        pilot synonym. This is the "I want to undo that" call: a pilot who lined
        up but wants to go back to holding short, or who is on final and wants to
        go around.
        """
        back = {
            Phase.CLEARANCE: Phase.IDLE,
            Phase.TAXI: Phase.IDLE,
            Phase.HOLDING: Phase.TAXI,
            Phase.LINEUP: Phase.HOLDING,
            Phase.TAKEOFF: Phase.HOLDING,
            Phase.AIRBORNE: Phase.AIRBORNE,
            Phase.INBOUND: Phase.AIRBORNE,
            Phase.LANDING: Phase.INBOUND,
        }.get(pilot.phase, Phase.IDLE)
        pilot.phase = back
        pilot.cleared_landing = False
        pilot.break_cleared = False
        pilot.pattern_flown = False
        pilot.descend_issued = False
        pilot.descend_intermediate = False
        pilot.awaiting_takeoff = False
        pilot.awaiting_landing = False
        pilot.lineup_readback = False
        pilot.descend_readback = False
        pilot.landing_handoff_announced = False
        return self._say("cancel_ack", callsign,
                         agency=self._agency(controller))

    # ---------- Ground ----------

    def _handle_ground(self, callsign: str, pilot: PilotState,
                       low: str, track: AircraftTrack | None,
                       traffic: list | None = None) -> str | None:
        # A handed-off pilot still on Ground ("Kutaisi Tower, ready to takeoff"
        # landed on Ground's frequency — the pilot dialled the wrong channel).
        # Re-issue the handoff instead of "say again"; any Ground-freq call in
        # the Holding phase is a wrong frequency by definition (Ground has
        # already told them to contact Tower).
        if pilot.phase == Phase.HOLDING:
            return self._say("contact_tower", callsign)
        # Readback of the runway-in-use/QNH info ("25 in use, QNH 2992") ->
        # "readback correct, advise when ready for clearance". Without this the
        # line has no trigger (none of {information, taxi, clearance}) and the
        # pilot gets "say again", stalling the flow at the very first exchange.
        if pilot.phase == Phase.IDLE and pilot.info_issued and re.search(
                r"\b(in use|qnh|q\.?n\.?h\.?)\b", low):
            pilot.info_issued = False
            return self._say("ground_info_readback", callsign)
        # Readback of the departure clearance ("after departure turn right Exit
        # North, 1500 ft or below") -> "readback correct". The pilot may read
        # back the gate name ("Northwest") without saying "Exit", so also accept
        # a configured gate name (the STT often drops the "Exit" prefix).
        if pilot.phase == Phase.CLEARANCE and (
                re.search(r"\b(after departure|turn|exit|1500|or below|below)\b",
                          low)
                or any(re.search(rf"\b{re.escape(gate.lower())}\b", low)
                       for gate in self.gates)):
            # Verify the readback against the clearance actually issued (kept in
            # last_clearance), then confirm — flagging a miss, never blocking.
            return self._check_readback(callsign, pilot, low,
                                        pilot.last_clearance or pilot.last_reply,
                                        Controller.GROUND)
        # A clearance *request*: "ready to copy clearance", "request clearance",
        # "requesting IFR". Distinguished from a *readback* (handled above) by
        # the request verb, so "cleared taxi" / "request taxi" no longer fall in
        # here and get a fresh departure clearance by mistake. A call that is
        # about *taxi* clearance ("request taxi clearance") is left to the taxi
        # handler below, which answers with the taxi clearance.
        if re.search(r"\b(request(?:ing)?|ask(?:ing)? for|ready to copy|"
                     r"ready for|like|need|want|copy)\b[^.]*\b"
                     r"(clearance|ifr|clearance delivery)\b", low) \
                and not re.search(r"\btaxi", low):
            pilot.phase = Phase.CLEARANCE
            gate = self._pick_exit_gate(traffic)
            pilot.exit_gate = gate
            turn = "right"
            if self.airfield is not None:
                turn = self.airfield.exit_turn(gate, self.runway)
            key = "departure_exit" if turn else "departure_exit_straight"
            reply = self._say(key, callsign, gate=self._exit_label(gate),
                              turn=turn)
            # A new clearance starts a fresh readback check: a miss
            # flagged on the *previous* clearance must not make the first
            # correct readback of this one look like a correction.
            pilot.readback_fails = 0
            pilot.last_clearance = reply
            return reply
        # Post-landing: taxi to parking (to a named ramp, or the nearest one).
        # Must come before the taxi readback, which also matches "taxi".
        # Guarded by `parking_cleared`: the readback ("cleared taxi to ramp
        # north") contains "to ramp" and would otherwise re-issue the clearance
        # forever.
        if not pilot.parking_cleared and re.search(
                r"\b(taxi to parking|to parking|to the ramp|to ramp|"
                r"taxi to the ramp|request(?:ing)?\s+(?:parking|"
                r"taxi\s+to\s+(?:parking|the ramp)))\b", low):
            pilot.phase = Phase.TAXI
            pilot.parking_cleared = True
            parking = self._spoken_parking(low)
            if parking is None and track is not None and self.airfield is not None:
                parking = self.airfield.nearest_parking_area(track.lat, track.lon)
            route = None
            if self.airfield is not None:
                lat = track.lat if track is not None else None
                lon = track.lon if track is not None else None
                route = self.airfield.parking_route(lat, lon)
            reply = self._say("taxi_parking", callsign,
                              parking=parking or "the ramp",
                              taxi_route=route or "Whiskey")
            # Remember this clearance so the pilot's readback is checked against
            # it — not the previous (departure) taxi clearance, which named a
            # runway and would make a correct parking readback look incomplete.
            # A new clearance starts a fresh readback check: a miss
            # flagged on the *previous* clearance must not make the first
            # correct readback of this one look like a correction.
            pilot.readback_fails = 0
            pilot.last_clearance = reply
            return reply
        # Readback of the taxi clearance ("cleared taxi Sierra Echo and hold
        # short runway 25") -> "readback correct". Must come before the taxi
        # request and the hold-short report (which also say "hold short"). A
        # parking readback may name only the ramp ("Ramp North, Alpha November")
        # with no keyword, so a named parking area also counts.
        if pilot.phase == Phase.TAXI and (
                re.search(r"\b(clear|cleared|copy|roger|via|taxi|"
                          r"hold short|runway)\b", low)
                or self._spoken_parking(low)) \
                and not re.search(r"\bholding\b", low):
            return self._check_readback(callsign, pilot, low,
                                        pilot.last_clearance or pilot.last_reply,
                                        Controller.GROUND)
        if re.search(r"\b(request(?:ing)?|asking for|like)\b.*\btaxi(?:ing)?\b"
                     r"|\btaxi(?:ing)?\b.*\b(startup|start up|start|runway)\b"
                     r"|\b(request(?:ing)?|ready)\b[^.]*\b(start(?:-?up)?|"
                     r"pushback|push back|engine start)\b"
                     r"|\bready\b[^.]*\btaxi(?:ing)?\b"
                     r"|\btaxi\b(?:\s*clearance)?\s*$",
                     low):
            pilot.phase = Phase.TAXI
            # Taxi route to the ACTIVE runway from the aircraft's ramp (routes
            # are configured per runway + ramp; DCS has no taxiway names).
            route = None
            if self.airfield is not None:
                lat = track.lat if track is not None else None
                lon = track.lon if track is not None else None
                route = self.airfield.taxi_route(lat, lon, self.runway)
            reply = self._say("taxi", callsign, taxi_route=route or "Sierra Echo")
            # A new clearance starts a fresh readback check: a miss
            # flagged on the *previous* clearance must not make the first
            # correct readback of this one look like a correction.
            pilot.readback_fails = 0
            pilot.last_clearance = reply
            return reply
        # "runway vacated" / "rolling off runway 25" reported on Ground (the
        # Tower->Ground handoff already happened at touchdown, so the pilot is
        # on Ground by the time they are clear). Just acknowledge — there is no
        # Ground action for it. Both MA's "rolling off" and the standard
        # "vacated" are accepted.
        if re.search(r"\b(vacated|clear of the runway|off the runway|"
                     r"runway vacated|clear of runway|rolling off|"
                     r"rolling out)\b", low):
            return self._say("roger", callsign, agency=self.ground_short)
        if re.search(r"\bhold(?:ing)? short\b", low) and pilot.phase == Phase.TAXI:
            # Cross-check the report against the live position: a pilot who
            # claims to be holding short but is still on the ramp gets
            # challenged, and their state does NOT advance.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_holding_short(track.lat, track.lon):
                return self._challenge(callsign, self.ground_short, track)
            pilot.phase = Phase.HOLDING
            # Match the Master Arms kneeboard: just hand the pilot to Tower
            # ("contact Tower on channel 7"). The holding position is still
            # cross-checked above (a pilot not actually at a holding point is
            # challenged), but it is not named in the reply — the kneeboard
            # dialog does not name it.
            return self._say("contact_tower", callsign)
        # Bare runup/hold-short follow-up: a pilot who got "say again" on a
        # readback may answer with just the missing item ("runway 07", 12:50:10
        # in the pancake debrief). If that item actually completes the previous
        # clearance, accept it instead of another "say again" that stalled the
        # flow and left the pilot re-asking.
        if (pilot.phase in (Phase.TAXI, Phase.CLEARANCE)
                and pilot.readback_fails > 0
                and self._completes_clearance(low, pilot)):
            pilot.readback_fails = 0
            return self._say("roger", callsign, agency=self.ground_short)

        # Initial check-in: "Ground, Adder11" -> "Adder11, Ground". A position/
        # formation report ("two-ship Hornets on Ramp South") gets the runway and
        # QNH. MA SOP: if the pilot did NOT listen to ATIS (no "with information
        # X"), Ground also reads out the weather ("weather is CAVOK. Wind 270,
        # 2 knots, Runway 25 in use, QNH 2992."); with the information letter the
        # call is just runway/QNH.
        if re.search(r"\b(ramp|apron|parking|taxiway|ship|hornets?|vipers?|"
                     r"information)\b", low):
            pilot.info_issued = True
            if re.search(r"\binformation\b", low):
                return self._say("ground_info", callsign)
            return self._say("ground_info_weather", callsign)
        if re.search(r"\bground\b", low):
            return self._say("ground_ack", callsign)
        return None

    # ---------- Tower ----------

    def _handle_tower(self, callsign: str, pilot: PilotState, low: str,
                      track: AircraftTrack | None,
                      traffic: list | None = None,
                      speaker: str = "") -> str | None:
        # "ready for departure/takeoff" is unambiguous; a bare "ready" only
        # counts once Ground has advanced the phase (avoids false positives).
        # Master Arms: Tower answers with "line up and wait"; the takeoff
        # clearance follows the pilot's readback (see handle()).
        if re.search(r"\bready for departure\b|\bready for takeoff\b|\bready to go\b",
                     low):
            # An *arrival* never asks for departure — a pilot who does is on the
            # wrong call (or a mis-parse); guide them to the arrival report
            # instead of wrongly clearing a takeoff.
            if pilot.phase in (Phase.INBOUND, Phase.LANDING):
                return self._say("arrival_wrong_call", callsign)
            # Already cleared for takeoff: re-issue the clearance (idempotent)
            # rather than dropping the pilot back to Lineup.
            if pilot.phase == Phase.TAKEOFF:
                pilot.awaiting_takeoff = False
                return self._takeoff_clearance(callsign, pilot)
            # Already lined up and waiting: this "ready" call is the natural
            # trigger for the takeoff clearance (the pilot has done the line-up
            # and is telling us they are ready to go). Without this, a pilot who
            # reports ready again instead of reading back "line up and wait"
            # would be stuck in Lineup forever.
            if pilot.phase == Phase.LINEUP:
                # Already lined up: this "ready" call is the natural trigger for
                # the takeoff clearance. Still sequence against traffic, and
                # still wait until the aircraft is actually lined up.
                if self._runway_busy(traffic, exclude=speaker):
                    pilot.awaiting_takeoff = True
                    pilot.takeoff_ready_announced = False
                    return self._say("hold_short_traffic", callsign)
                if not self._lined_up(track):
                    pilot.lineup_readback = True
                    return ""
                pilot.phase = Phase.TAKEOFF
                pilot.awaiting_takeoff = False
                return self._takeoff_clearance(callsign, pilot)
            # Cross-check: a pilot who says "ready for departure" but is not at
            # the runway gets challenged, and is NOT cleared to line up.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_holding_short(track.lat, track.lon):
                return self._challenge(callsign, self.tower_short, track)
            # Traffic: do not let them line up if the runway is occupied.
            if self._runway_busy(traffic, exclude=speaker):
                pilot.phase = Phase.HOLDING
                # Held: remember it so the bot calls them back, unprompted, when
                # the runway clears (see check_runway_clear).
                pilot.awaiting_takeoff = True
                pilot.takeoff_ready_announced = False
                return self._say("hold_short_traffic", callsign)
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        if re.search(r"\bready\b", low) and pilot.phase in (
                Phase.TAXI, Phase.HOLDING, Phase.CLEARANCE, Phase.LINEUP):
            if pilot.phase == Phase.LINEUP:
                # already lined up: a bare "ready" also clears takeoff
                if self._runway_busy(traffic, exclude=speaker):
                    pilot.awaiting_takeoff = True
                    pilot.takeoff_ready_announced = False
                    return self._say("hold_short_traffic", callsign)
                if not self._lined_up(track):
                    pilot.lineup_readback = True
                    return ""
                pilot.phase = Phase.TAKEOFF
                pilot.awaiting_takeoff = False
                return self._takeoff_clearance(callsign, pilot)
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        # Holding short report addressed to Tower (the pilot may switch frequency
        # before Ground hands them over, or simply repeat where they are). Tower
        # owns the runway, so answer the report rather than "say again" (debrief
        # 2026-10-22: "Tower, Epic 4, holding short runway 07" was rejected,
        # leaving the pilot at the hold point with no way to get a clearance).
        if pilot.phase in (Phase.TAXI, Phase.HOLDING) and re.search(
                r"\bhold(?:ing)? short\b", low):
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_holding_short(track.lat, track.lon):
                return self._challenge(callsign, self.tower_short, track)
            if self._runway_busy(traffic, exclude=speaker):
                pilot.phase = Phase.HOLDING
                pilot.awaiting_takeoff = True
                pilot.takeoff_ready_announced = False
                return self._say("hold_short_traffic", callsign)
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        # A go-around is a real state transition, not an optional pattern
        # status call. Acknowledge it and re-arm the next landing clearance.
        if re.search(r"\b(go(?:ing)? around|missed approach)\b", low):
            repeated = pilot.go_around_issued
            pilot.phase = Phase.INBOUND
            pilot.cleared_landing = False
            pilot.pattern_flown = False
            pilot.go_around_issued = True
            pilot.landing_handoff_announced = False
            if repeated:
                return ""
            return self._say("go_around_ack", callsign)
        # A readback of the approval is not a second touch-and-go request.
        if re.search(r"\btouch(?:\s+|-)and(?:\s+|-)go\b", low) \
                and re.search(r"\b(approved|cleared)\b", low) \
                and not re.search(r"\b(request|requesting|clearance)\b", low):
            return ""
        # Touch-and-go is an explicit landing clearance request. Sequence it
        # against runway traffic, then re-arm the next circuit's landing flow.
        if re.search(r"\btouch(?:\s+|-)and(?:\s+|-)go\b", low) \
                and re.search(r"\b(request|requesting|clearance)\b",
                              low):
            if pilot.phase not in (Phase.INBOUND, Phase.LANDING):
                return None
            if self._runway_busy(traffic, exclude=speaker):
                pilot.phase = Phase.INBOUND
                pilot.cleared_landing = False
                pilot.pattern_flown = False
                pilot.go_around_issued = True
                return self._say("go_around_traffic", callsign)
            pilot.phase = Phase.INBOUND
            pilot.cleared_landing = False
            pilot.pattern_flown = False
            pilot.landing_handoff_announced = False
            return self._say("touch_and_go_approved", callsign)
        # Accept a spoken exit report while still on Tower. A valid report
        # beyond the matching outbound arc can complete the automatic handoff;
        # an early report gets a useful instruction instead of "say again".
        spoken_exit = self._spoken_gate(low)
        if (pilot.phase == Phase.TAKEOFF and spoken_exit
                and re.search(r"\b(exit|passing|passed|crossing)\b", low)):
            gate = spoken_exit.split()[-1]
            if (track is not None and self.airfield is not None
                    and gate in self.airfield.departure_gates(self.runway)
                    and self.airfield.gate_arc_passed_outbound(
                        track.lat, track.lon, gate)):
                pilot.phase = Phase.AIRBORNE
                pilot.departure_handoff_announced = True
                pilot.departure_previous_range_nm = None
                pilot.departure_previous_bearing_deg = None
                note = self._exit_deviation_note(pilot, gate)
                return note + self._say("contact_control", callsign)
            return ""
        # Departure handoff: once airborne, Tower hands the flight to Control.
        # "rolling" alone is NOT a departure call — a landing pilot says
        # "rolling off runway 25" (handled below), so require a departure word.
        # "wheels up" is the MA departure report; guard it against landing
        # phrases ("wheels up on the go" after a touch-and-go is not one).
        if re.search(r"\b(airborne|departing|leaving|departed|on the way out|"
                     r"taking off|wheels up|off the ground)\b", low) \
                and not re.search(r"\b(vacated|rolling off|rolling out|"
                                  r"touch and go|on the go|go around)\b", low):
            if pilot.phase == Phase.TAKEOFF:
                handoff = self.check_departure_handoff(callsign, track)
                if handoff:
                    return handoff
                return ""
            pilot.phase = Phase.AIRBORNE
            return self._say("contact_control", callsign)
        # Readback of "report runway in sight" ("Report runway in sight, Adder11")
        # is NOT the runway-in-sight report itself — only the actual report (said
        # when the runway is genuinely in sight) triggers the break clearance.
        # Must come before the runway-in-sight branch. STT also drops the "in"
        # ("report runway sight", debrief 2026-10-18 13:06:55), so accept the
        # mangled form too — "runway sight" is never a position report.
        if re.search(r"\b(report|reporting|wilco|roger|copy)\b.*"
                     r"\brunway (?:in )?sight\b", low):
            return self._say("roger", callsign, agency=self.tower_short)
        if re.search(r"\b(runway in sight|runway insight|visual)\b", low):
            pilot.phase = Phase.LANDING
            pilot.break_cleared = True
            # Master Arms: the break clearance is valid for the whole flight
            # (same as the landing clearance — see the OHB branch below).
            for wing in self._flight_members(callsign):
                wing_state = self.pilots.get(wing)
                if wing_state is not None and wing_state.phase in (
                        Phase.INBOUND, Phase.LANDING):
                    wing_state.break_cleared = True
            return self._say("cleared_overhead", callsign,
                             flight=self._formation_prefix(pilot))
        # Tower asks for a position after check-in. Accept an assigned entry
        # report, a named marking/BRA report, or a plain location report; use
        # radar to decide whether the aircraft has reached its entry point.
        gate = (self._spoken_gate(low) or pilot.entry_gate
                or (self._pick_entry_gate(track, None, pilot)
                    if pilot.phase in (Phase.INBOUND, Phase.LANDING) else ""))
        is_entry_report = (re.search(r"\bentry\b", low) is not None
                           or (pilot.phase in (Phase.INBOUND, Phase.LANDING)
                               and self._spoken_gate(low) is not None))
        is_marking_report = (re.search(
            r"\b(marking|bullseye|bull'?s?eye|bulls?)\b", low)
            is not None)
        is_location_report = (re.search(
            r"\b(position|over|at|abeam|north|south|east|west)\b", low)
            is not None)
        if (pilot.phase in (Phase.INBOUND, Phase.LANDING)
                and (is_entry_report or is_marking_report or is_location_report)):
            already_in_ctr = bool(
                track is not None and self.airfield is not None
                and self.airfield.ctr.contains_horizontal(track.lat, track.lon))
            if is_marking_report:
                # A complete BRA is checked against its named reference when
                # available. A short "marking bulls/Kutaisi" still counts as
                # a position report because radar supplies the actual fix.
                off = self._verify_picture(callsign, low, track)
                if off:
                    return self._say("tower_position_bra_challenge", callsign,
                                     agency=self.tower_short, **off)
            if (is_entry_report and gate and not already_in_ctr
                    and not self._at_entry(track, gate)):
                if track is not None:
                    return self._challenge(callsign, self.tower_short, track)
                return self._say("tower_continue_entry", callsign, gate=gate)
            if (not is_entry_report and gate and not already_in_ctr
                    and not self._at_entry(track, gate)):
                return self._say("tower_continue_entry", callsign, gate=gate)
            pilot.phase = Phase.LANDING
            pilot.cleared_inbound = True
            return self._say("report_runway_in_sight", callsign)
        # Overhead-break calls. "overhead break" / "initial" is the request for
        # the break clearance. "in the break" and "turning final" are optional
        # position reports (MA: in the break if traffic nearby; turning final
        # is good practice). None of them is required — "on final" still
        # clears to land if they skip the pattern calls.
        if re.search(r"\b(overhead br(eak|ake)|initial)\b", low) \
                and not re.search(r"\bcleared\b", low):
            pilot.phase = Phase.LANDING
            if pilot.break_cleared:
                # Readback of the break clearance without "cleared" (STT also
                # writes "brake"). Silence, same as a full readback.
                return ""
            pilot.break_cleared = True
            # Master Arms: the break clearance is valid for the whole flight.
            # Mark same-flight wingmen already in the arrival flow as covered,
            # so their own pattern calls/radar flow work without an
            # individual break clearance (mirrors _issue_landing_clearance).
            for wing in self._flight_members(callsign):
                wing_state = self.pilots.get(wing)
                if wing_state is not None and wing_state.phase in (
                        Phase.INBOUND, Phase.LANDING):
                    wing_state.break_cleared = True
            return self._say("cleared_overhead", callsign,
                             flight=self._formation_prefix(pilot))
        # Readback of the break clearance ("cleared for right overhead break")
        # must not look like "in the break". Correct readback is silent.
        if pilot.phase == Phase.LANDING and re.search(r"\bcleared\b", low) \
                and re.search(r"\b(overhead|break|brake|runway)\b", low) \
                and not re.search(r"\b(to land|on final|in the br(eak|ake))\b", low):
            pilot.break_cleared = True
            return ""
        if re.search(r"\b(?:in the br(eak|ake)|on the br(eak|ake)|"
                     r"in br(eak|ake)|br(?:eak|ak)ing)\b", low):
            # Position report only (MA: if traffic nearby). Not a landing
            # request and not a cue to skip "turning final" — stay silent.
            pilot.phase = Phase.LANDING
            return ""
        # Pattern position reports (MA: optional, no reply). Pilots fly and
        # report the circuit differently — "turning base", "on downwind",
        # "upwind", "crosswind" — and none of them is a landing request or a
        # cue to skip "on final". One family, so adding a variant later is a
        # one-word change. "turning final" is included here (it must not fall
        # through to the bare-"final" landing clearance below).
        if re.search(r"\b(up ?wind|cross ?wind|down ?wind|"
                     r"base (?:leg|turn)?|turn(?:ing)?\s+(?:to\s+|onto\s+)?"
                     r"base|turn(?:ing)?(?:\s+to|\s+onto)?\s+final)\b", low):
            pilot.phase = Phase.LANDING
            return ""
        # Readback of the takeoff clearance once already cleared ("right turn
        # out, cleared for takeoff, 33, Colt 1"). Must come before the "final"
        # check, so a readback containing "turn" is not misread as a landing.
        # Master Arms: after the takeoff clearance readback there is no extra
        # call — the next transmission is the airborne handoff.
        if pilot.phase == Phase.TAKEOFF and re.search(
                r"\b(cleared|takeoff|take off|right turn|left turn|turnout)\b",
                low):
            return ""
        # After landing, Tower hands the flight to Ground. Normally the bot
        # pushes the pilot to Ground *unprompted* once they roll off the runway
        # (check_landing_handoff); this branch covers a pilot who reports
        # "vacated" (or MA's "rolling off runway 25") first. Idempotent: if the
        # handoff was already given, just acknowledge rather than repeating
        # "contact Ground".
        if re.search(r"\b(vacated|clear of the runway|off the runway|"
                     r"runway vacated|clear of runway|rolling off|"
                     r"rolling out)\b", low):
            if pilot.landing_handoff_announced:
                return self._say("roger", callsign, agency=self.tower_short)
            pilot.landing_handoff_announced = True
            pilot.phase = Phase.TAXI
            return self._say("contact_ground", callsign)
        # Readback of the landing clearance ("runway 25, cleared to land") once
        # already cleared. Master Arms (Landing Procedures §Comms): "after the
        # landing clearance (and your readback) there's generally no need for
        # additional comms" — so the bot stays SILENT (no "roger"). The branch
        # still exists so the readback is not misrouted to another intent.
        # "" is the deliberate-silence convention (see workers._handle).
        # A wingman covered by the lead's clearance (same flight, lead already
        # cleared to land) is in the same boat: their "on final" needs no
        # individual clearance, so it is acknowledged with silence too.
        if pilot.phase == Phase.LANDING and re.search(
                r"\b(cleared|clear to land|copy|roger|wilco|readback)\b", low):
            return ""
        if pilot.phase == Phase.INBOUND and pilot.cleared_landing \
                and re.search(r"\b(on final|short final|final)\b", low):
            # Covered by the lead's clearance: the wingman is effectively
            # cleared, so advance them to Landing (the post-landing handoff
            # and go-around logic then treat them like any cleared arrival).
            pilot.phase = Phase.LANDING
            return ""
        # Readback of "report on final" (the in-the-break cue), not the
        # position report. "reporting on final" / "on final" still land.
        if re.search(r"\breport on final\b", low) \
                and not re.search(r"\breporting\b", low):
            return ""
        # "on final" is a landing clearance (distinct from a general inbound call).
        # Bare "final" is accepted so a clipped "Adder 1 final" still works;
        # "turning final" already returned above.
        if re.search(r"\b(on final|short final|final)\b", low):
            # After the clearance (voice or radar), "on final" is just a
            # status report — the pilot confirming what we already cleared.
            # Stay silent (Master Arms: no extra comms after the readback).
            if pilot.cleared_landing:
                return ""
            # Zones check: a pilot still in the pattern turn (inside a slice)
            # has NOT rolled out yet — their "on final" does not match the
            # radar picture. The SOP answer to a position report that does
            # not match reality is the position challenge (ATC.md §"on final"
            # not on final); no invented "continue" call.
            if track is not None and self.airfield is not None \
                    and self.airfield.in_pattern_turn(track.lat, track.lon,
                                                      self.runway) \
                    and not self.airfield.is_established_final(
                        track.lat, track.lon, track.heading, self.runway):
                return self._challenge(callsign, self.tower_short, track)
            # Cross-check: only clear to land if actually on final.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_on_final(track.lat, track.lon,
                                                      track.heading,
                                                      self.runway,
                                                      max_heading_offset_deg=(
                                                          ON_FINAL_REPORT_HEADING_TOLERANCE_DEG)):
                return self._challenge(callsign, self.tower_short, track)
            # Traffic: if the runway is occupied, sequence instead of clearing.
            if self._runway_busy(traffic, exclude=speaker):
                pilot.phase = Phase.INBOUND
                # Held on approach: the bot re-clears when the runway clears.
                pilot.awaiting_landing = True
                pilot.landing_ready_announced = False
                return self._say("continue_approach", callsign)
            return self._issue_landing_clearance(callsign, pilot)
        if re.search(r"\binbound\b|\bon approach\b|\blanding\b|\bentry\b|"
                     r"\brequest(?:ing)?\s+(?:to\s+)?land\b|\bready to land\b|"
                     r"\brequest\s+landing\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            return self._inbound_reply(callsign, pilot, track, low)
        return None

    # ---------- Control ----------

    def _handle_control(self, callsign: str, pilot: PilotState, low: str,
                        track: AircraftTrack | None) -> str | None:
        if pilot.pending_handoff_to == Controller.TOWER.value:
            return self._say("control_handoff_repeat", callsign)
        # Already handed off to Tower (Landing): Control has nothing further to
        # say. Without this, a later Control call parsed as an inbound/entry
        # report re-cleared a join and pulled the arrival back off Tower — the
        # 3-minute Control/Tower ping-pong in the 2026-10-22 sortie. Repeat the
        # handoff so a pilot who missed it is never stranded.
        if pilot.phase == Phase.LANDING:
            return self._say("control_handoff_repeat", callsign)
        # Explicit entry request ("Control, Colt 1, request Entry North"):
        # approve the pilot's chosen gate and remember it, so the join and any
        # follow-up use it. A bare direction in an inbound call ("35 miles
        # north") is a *position*, not a request, so it does not override the
        # best entry (see `_requested_gate`).
        requested = self._requested_gate(low)
        if requested:
            # Approve the pilot's requested gate and remember it in the pilot
            # state (so the inbound/join and the map trail use it too).
            pilot.entry_gate = requested
            pilot.arrival_previous_range_nm = None
            pilot.arrival_previous_bearing_deg = None
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            heading = None
            if track is not None and self.airfield is not None:
                heading = self.airfield.join_heading(track.lat, track.lon,
                                                     requested)
            if heading is None:
                return self._say("control_join_requested_nohdg", callsign,
                                 gate=requested)
            turn = self._join_turn(track, heading)
            key = ("control_join_requested" if turn
                   else "control_join_requested_straight")
            return self._say(key, callsign, gate=requested,
                             heading=f"{self._cockpit_bearing(heading):03.0f}",
                             turn=turn)
        # Readback of the join clearance ("150 to join via Entry East") ->
        # Control issues the descent to 1500 ft (once). The Tower handoff is
        # position-driven at the assigned entry arc, with CTR entry as the
        # missed-arc fallback; descent issuance and readback do not gate it.
        # NOTE: a *request* for a heading/vector/azimuth is a trainer aid and is
        # matched in `_dispatch` BEFORE controller dispatch, so it never reaches
        # this readback branch. Only a genuine readback ("150 to join via Entry
        # East") gets here.
        if pilot.phase == Phase.INBOUND and re.search(
                r"\b(join|via|heading|turn|descend\w*|descent|maintain\w*|"
                r"level(?:ling|ling off)?|1500?|5000?)\b", low):
            if not pilot.descend_issued:
                # Step the descent by distance: far out -> an intermediate
                # altitude; close to the entry -> the final 1500 ft. The
                # final descent is part of the SOP, but handoff monitoring is
                # independent so a missed descent call cannot block transfer.
                # A pilot still above the step-down altitude gets silence here
                # (the descent plan is acknowledged; see _descend_reply) — but
                # their readback is remembered for the clearance state; the
                # handoff monitor independently watches the entry arc.
                if pilot.descend_readback:
                    # Already read back the descent plan. The monitor handles
                    # the assigned entry arc; CTR entry is its fallback.
                    return ""
                return self._descend_reply(callsign, pilot, track)
            pilot.descend_readback = True
            return ""
        # Already inbound / handed off, and the pilot reports being at or near
        # the entry point -> hand to Tower. NEVER re-issue the join here: once
        # the join has been given, an "entry" mention must not loop the arrival
        # back to the start (it did — any call containing "entry" re-issued the
        # join regardless of phase, so a handoff could be undone).
        if pilot.phase in (Phase.INBOUND, Phase.LANDING) and re.search(
                r"\b(passing|entering|at the entry|entry|established|abeam)\b",
                low):
            # Once Control has already handed them off, the arrival's phase is
            # Landing and the *next* agency is Tower: re-clearing or
            # re-challenging here would leave them stuck on Control (debrief
            # 2026-10-22: 3 minutes of Control/Tower ping-pong, and the sortie
            # ended with no landing clearance at all). Just re-state the
            # handoff, so a pilot who missed it is never stranded.
            if pilot.phase == Phase.LANDING:
                return self._say("control_handoff_repeat", callsign)
            # Cross-check the report against the live position: a pilot who
            # claims to be at the entry point but is still far away is
            # challenged, and is NOT handed to Tower (they would arrive
            # unannounced). The named gate is used if given, else the assigned
            # one. Without a live track we trust the pilot.
            gate = self._spoken_gate(low) or pilot.entry_gate
            if gate and not self._at_entry(track, gate):
                return self._challenge(callsign, self.control_short, track)
            pilot.pending_handoff_to = Controller.TOWER.value
            pilot.tower_handoff_announced = True
            return self._say("contact_tower_from_control", callsign)
        # First arrival check-in ("inbound ..."). Blocked once the flight has
        # been handed to Tower (Landing), so a stray "inbound" cannot loop the
        # arrival back; on the first call it issues the join, and a follow-up
        # inbound (still with Control) keeps the assigned/requested gate.
        # Arrival check-in takes priority over the departure keywords: it often
        # names an altitude ("inbound 35 miles north at Angels 12"), which must
        # NOT be read as a departure check-in (that would answer a joining
        # aircraft with "climb to Angels 15").
        if pilot.phase != Phase.LANDING and re.search(
                r"\binbound\b|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.tower_handoff_announced = False
            pilot.arrival_previous_range_nm = None
            pilot.arrival_previous_bearing_deg = None
            pilot.cleared_inbound = True
            gate = self._pick_entry_gate(track, low, pilot)
            pilot.entry_gate = gate
            # Compute the join heading from the pilot's live position (not a
            # canned value); fall back to the static variable without a track.
            heading = None
            if track is not None and self.airfield is not None:
                heading = self.airfield.join_heading(track.lat, track.lon, gate)
            if heading is None:
                return self._say("control_join_nohdg", callsign, gate=gate)
            turn = self._join_turn(track, heading)
            key = "control_join" if turn else "control_join_straight"
            return self._say(key, callsign, gate=gate,
                             heading=f"{self._cockpit_bearing(heading):03.0f}",
                             turn=turn)
        # Departure check-in ("airborne, 5 miles east climbing" / "at 1500 ft"):
        # radar contact and a climb clearance. Only the *first* check-in gets the
        # climb — a readback or a follow-up level report ("climbing angels 15",
        # "at angels 16") is acknowledged, not re-cleared, so the pilot is not
        # handed an endless "climb to Angels 15".
        if re.search(r"\b(airborne|climbing|climb|departing|departed|level|"
                     r"angels|on the way|at \d+|checking in|with you|"
                     r"ready to climb)\b", low):
            # A departure stays a departure: it must NOT be flipped to Inbound
            # here, or Control would treat the outbound leg as an arrival
            # (debrief 2026-10-22: the post-takeoff check-in moved the pilot
            # Lineup -> Inbound and the join flow then fought the departure
            # flow). The arrival flow is armed by an explicit inbound call.
            if pilot.phase not in (Phase.INBOUND, Phase.LANDING):
                pilot.phase = Phase.AIRBORNE
            pilot.control_checkin_received = True
            # A pilot who checks in with Control while still inside the CTR
            # (below the ceiling) is on the wrong frequency: Tower owns the
            # zone up to the ceiling. Real Control would not clear them up
            # through the zone — it tells them to stay low until clear. As a
            # trainer we guide them back rather than issue the climb.
            if self._inside_ctr(track):
                return self._say("control_too_early", callsign,
                                 gate=self._departure_gate(pilot))
            if pilot.climb_issued:
                # Climb readback / level report. MA SOP next step is the
                # Stingray handoff once they reach Angels 15 (radar, not a
                # second call) — stay silent, same as any readback that has
                # a next instruction. Do not "roger".
                return ""
            pilot.climb_issued = True
            return self._say("control_climb", callsign)
        # Readback of the "remain below 1500 ft until clear of the control
        # zone" instruction (a departure still inside the CTR), or a report at
        # the exit point -> acknowledge. Without this the readback fell through
        # to "say again" (it names no departure keyword).
        if pilot.phase == Phase.AIRBORNE and re.search(
                r"\b(remain|remaining|below|1500|clear of the control zone|"
                r"exit)\b", low):
            return self._say("roger", callsign, agency=self.control_short)
        # Near the field, Control hands the flight to Tower (e.g. "on final",
        # "runway in sight", "overhead break", "passing the entry point").
        # "request landing" belongs here too: a pilot who asks Control to land
        # is asking to be handed over, and answering "say again" left them
        # stranded on the wrong frequency (debrief 2026-10-22). It is the same
        # handoff the "on final" phrasing already got.
        if re.search(r"\b(on final|final|runway in sight|visual|overhead|"
                     r"break|initial|passing|entering|abeam|"
                     r"established\s+on\b|"
                     r"request(?:ing)?\s+(?:to\s+)?land|request\s+landing|"
                     r"landing)\b", low):
            pilot.pending_handoff_to = Controller.TOWER.value
            pilot.tower_handoff_announced = True
            return self._say("contact_tower_from_control", callsign)
        # Common short-hands for returning to base. A pilot who says "RTB" or
        # "returning to base" is inbound, and answering "say again" strands them.
        if re.search(r"\b(rtb|returning to (?:base|the field|the boat)|"
                     r"recover(?:ing|y))\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            gate = self._pick_entry_gate(track, low, pilot)
            pilot.entry_gate = gate
            return self._say("control_join_nohdg", callsign, gate=gate)
        # Initial Control check-in, bare: "Kutaisi Control, Adder11" (Master Arms
        # kneeboard page 2, step 1) -> "Adder11, Control". Answer it instead of
        # "say again"; the pilot follows with the inbound call.
        # NOTE: exclude "control zone" — a departure readback ("remain below
        # 1500 ft until clear of the control zone") contains "control" and was
        # wrongly treated as an arrival check-in, flipping the phase to Inbound.
        # Airborne pilots (post-departure, before/at the handoff to Control)
        # also say just "Control, callsign" when switching frequency — that is
        # a frequency-change check-in, not an inbound check-in. Don't flip
        # Airborne -> Inbound on it; just answer. The same guard covers a
        # check-in right AFTER the Tower handoff (debrief 2026-10-18 12:55:33):
        # once the join has been issued (cleared_inbound) a bare check-in must
        # not re-arm the join flow by flipping Landing -> Inbound.
        # A useful direction report ("marking both", "075 for 41") is a
        # fighter-to-fighter picture call, not a clearance request. Answering it
        # with "say again" trains the pilot to stop making the call (debrief
        # 2026-10-22: "marking bull, zero seven seven, four forty" was rejected
        # twice). We do not simulate the picture; acknowledge it.
        if re.search(r"\b(marking|bullseye|bull'?s?eye|picture|"
                     r"for \d{2,3}|radar contact)\b", low):
            return self._say("roger", callsign, agency=self.control_short)
        if re.search(r"\bcontrol\b(?!\s+zone)", low):
            if pilot.phase == Phase.AIRBORNE or pilot.cleared_inbound:
                return self._say("control_ack", callsign,
                                 agency=self.control_short)
            # A pilot who is already a *departure* (lined up, or just airborne)
            # is switching to Control for the climb, not starting an arrival.
            # Flipping them to Inbound here made Control treat the outbound leg
            # as an arrival, and the join flow then fought the departure flow
            # (debrief 2026-10-22: this is the check-in that moved the pilot
            # Lineup -> Inbound seconds after takeoff).
            if pilot.phase in (Phase.LINEUP, Phase.TAKEOFF):
                pilot.phase = Phase.AIRBORNE
                return self._say("control_ack", callsign,
                                 agency=self.control_short)
            pilot.phase = Phase.INBOUND
            return self._say("control_ack", callsign,
                             agency=self.control_short)
        return None

    def _handle_stingray(self, callsign: str, pilot: PilotState, low: str,
                         track: AircraftTrack | None) -> str | None:
        """Stingray (AWACS) on the package channel — Master Arms SOP.

        The tactical leg: check in, give a bullseye picture, then RTB. The
        handoff back to Control re-enters the normal arrival flow, so the
        inbound dialogs are unchanged. Authentication (RAMROD) is not
        implemented yet.
        """
        # RTB: every way a flight says "I am going home". The reason is never
        # questioned — a trainer flight may be a strike, a fuel check, or just
        # a pilot who wants to head back, and all of them get the same "copy,
        # RTB" and arm the Control handoff.
        #   Miller Time  ordnance delivered (mission complete)
        #   Bingo        fuel state to return
        #   Joker        fuel state to start heading home
        #   Bugout       leaving the engagement
        #   Winchester   no ordnance remaining
        #   RTB / returning to base / recovering  plain English
        if re.search(r"\b(miller time|bingo|joker|bugout|bug out|winchester|"
                     r"rtb|returning to (?:base|the field|the boat)|"
                     r"request(?:ing)?\s+(?:to\s+)?(?:rtb|return)|"
                     r"recover(?:ing|y))\b", low):
            repeated = pilot.rtb_requested
            pilot.phase = Phase.STINGRAY
            pilot.rtb_requested = True
            if not repeated:
                pilot.rtb_control_handoff_announced = False
                pilot.rtb_control_handoff_not_before = (
                    time.monotonic() + RTB_CONTROL_HANDOFF_DELAY_SEC)
            if repeated:
                return ""
            return self._say("stingray_rtb_copy", callsign)

        # A bullseye picture call ("Bullseye 065 for 40, Angels 15, as fragged").
        # Verified against the mission's own bullseye when we have it; a call
        # that is off gets a coaching challenge (never a block — the pilot keeps
        # flying, exactly like the position cross-check). Repeatable: a pilot
        # practising bullseye calls can make as many as they like.
        if re.search(r"\b(bullseye|bull'?s?eye|bulls?|marking)\b", low):
            # An air-start (or any pilot who never checked in elsewhere) is on
            # the tactical leg by definition once they talk to Stingray, so the
            # map shows the right phase without a full departure first.
            if pilot.phase in (Phase.IDLE, Phase.AIRBORNE):
                pilot.phase = Phase.STINGRAY
            return self._bullseye_reply(callsign, pilot, low, track)

        # Handoff back to Control ("request Control", "requesting channel 8",
        # "request landing", "inbound"). Stingray does not control the field, so
        # any of these means "I want to come home" — give the Control handoff,
        # which re-enters the normal arrival flow (the inbound dialogs are
        # reused). A pilot may ask to move on at any time; the trainer never
        # makes them wait for the controller to offer it.
        if re.search(r"\b(control|channel\s*8|channel eight|inbound|"
                     r"request(?:ing)?\s+(?:to\s+)?(?:land|landing|approach)|"
                     r"approach)\b", low):
            return self._say("stingray_contact_control", callsign)

        # Package-channel reports that need no answer from AWACS: the flight
        # pushing off the hold ("Adder1 pushing") and ordnance away ("one
        # away"). Both are SOP calls on this channel; answering "say again"
        # would train the pilot to stop making them. Stay silent — they are
        # reports to the package, not to the controller.
        if re.search(r"\b(pushing|push(?:ing)? time|one away|two away|"
                     r"rifle|fox\s*(?:one|two|three)|magnum)\b", low):
            return ""

        # Tactical readbacks do not need another controller transmission.
        # "ASFRAG" is a common clipped STT form of "as fragged".
        if re.search(r"\b(as\s*frag(?:ged)?|fragged)\b", low):
            return ""
        if re.search(r"\b(copies|copy(?: that)?|roger(?: that)?|wilco)\b",
                     low):
            return ""

        # Bare check-in ("Stingray, Adder11") -> "Adder11, Stingray". An
        # air-start (or a pilot who never checked in elsewhere) is on the
        # tactical leg by definition once they talk to Stingray, so the map
        # shows the right phase without a full departure first.
        #
        # Deliberately *only* a genuine bare check-in: the old guard also
        # matched any call merely containing "stingray"/"awacs", so every
        # unrecognised call ("Stingray, Colt 1, picture") got a false
        # "Colt 1, Stingray." ack instead of falling through to its own
        # handling. A call that carries content is not a check-in.
        if self._is_bare_checkin(low, ""):
            if pilot.phase in (Phase.IDLE, Phase.AIRBORNE):
                pilot.phase = Phase.STINGRAY
            return self._say("stingray_ack", callsign)

        return None

    def _cockpit_bearing(self, bearing: float) -> float:
        """A geodesic-true bearing converted to the cockpit's display
        convention (true − magnetic variation − DCS map rotation; see
        airspace.py Airfield.magnetic_variation_deg/map_rotation_deg)."""
        return (bearing + self.cockpit_bearing_offset_deg) % 360

    def _bullseye_reply(self, callsign: str, pilot: PilotState, low: str,
                        track: AircraftTrack | None) -> str:
        """Acknowledge a bullseye/marking picture call, coaching if it is off.

        The pilot reads bullseye off their HSI, so the check is tolerant
        (BULLSEYE_TOLERANCE_DEG / _NM) and never blocks: an off call gets the
        real bearing/range so the pilot can correct, and the flight continues.
        The stated angels is checked too (a picture call includes the altitude),
        with the same tolerance. Without a mission bullseye (or a live track) we
        simply acknowledge.

        Two trainer-specific cases on top of the plain check:

        * An **exact-altitude** statement ("angels 15703", "15703 tf") is a
          format error, not a position error — the SOP wants whole thousands
          ("angels 16"). It is coached explicitly, because reading the digits
          off the altimeter is exactly the habit the trainer exists to break.
        * A **marking** call names a point ("marking Kutaisi two zero zero for
          twenty"). The point is resolved against the served airfield and the
          bullseye; a point we do not know is acknowledged without a check
          rather than guessed at.
        """
        exact = parse_exact_altitude(low)
        if exact is not None:
            # The altitude format was wrong; the position may still be off, so
            # combine both into one transmission (two "negative"s in a row would
            # not be said on the radio).
            rounded = f"{round(exact / 1000.0):.0f}"
            off = self._verify_picture(callsign, low, track)
            if off:
                return self._say("stingray_angels_and_position", callsign,
                                 angels=rounded, **off)
            return self._say("stingray_angels_rounding", callsign,
                             angels=rounded)
        off = self._verify_picture(callsign, low, track)
        if off:
            return self._say("stingray_bullseye_challenge", callsign, **off)
        return self._say("stingray_radar_contact", callsign)

    def _verify_picture(self, callsign: str, low: str,
                        track: AircraftTrack | None) -> dict | None:
        """Check the position in a bullseye/marking call.

        Returns the challenge fields (``point``/``bearing``/``range``/
        ``angels``) when the position is off, or None when it checks out or
        states no position we can verify (the caller then acknowledges).
        """
        if track is None:
            return None
        marking = parse_marking_call(low)
        if marking is not None:
            name, reported_bearing, reported_range = marking
            reported_variants = _picture_bra_variants(reported_bearing,
                                                       reported_range)
            point = self._resolve_point(name)
        else:
            reported_variants = _picture_call_variants(low)
            if not reported_variants:
                return None
            point = self._resolve_point("bullseye")
        if point is None:
            return None
        label, (plat, plon) = point
        bearing, distance = bearing_distance(plat, plon, track.lat, track.lon)
        # The pilot reports BRA in cockpit units (true − variation − rotation,
        # see _cockpit_bearing); the challenge answers the same way.
        bearing = self._cockpit_bearing(bearing)
        # STT sometimes joins a spoken "four" to the next figure: "for 57"
        # becomes 457 and "for 40" becomes 440. Consider those interpretations
        # only when the aircraft's actual BRA confirms one within tolerance.
        matched = next((candidate for candidate in reported_variants
                        if abs((candidate[0] - bearing + 180) % 360 - 180)
                        <= BULLSEYE_TOLERANCE_DEG
                        and abs(candidate[1] - distance)
                        <= BULLSEYE_TOLERANCE_NM), None)
        # Angels: the call states the altitude in thousands of feet. Only
        # checked when the pilot actually said it (it is optional in the SOP
        # call), and only when we have a live altitude.
        reported_angels = parse_angels(low)
        actual_angels = track.alt_ft / 1000.0
        off_angels = (abs(reported_angels - actual_angels)
                      if reported_angels is not None else 0.0)
        if (matched is not None
                and off_angels <= ANGELS_TOLERANCE_FT / 1000.0):
            return None
        return {"point": label, "bearing": f"{bearing:03.0f}",
                "range": f"{distance:.0f}",
                "actual_angels": f"{actual_angels:.0f}"}

    def _resolve_point(self, name: str) -> tuple[str, tuple[float, float]] | None:
        """Resolve a picture-call point name to (label, (lat, lon)), or None.

        Only points the trainer actually knows: the coalition bullseye and the
        served airfield (the SOP example names the field — "marking Kutaisi two
        zero zero for twenty"). Anything else returns None, so the call is
        acknowledged without a check instead of being verified against a guess.
        """
        if name == "bullseye":
            if self.bullseye is None:
                return None
            return "bullseye", self.bullseye
        if self.airfield is None:
            return None
        label = self.airfield.name
        known = {label.lower()}
        if label.lower() == "kutaisi":
            known.add("kutasi")  # common STT spelling of Kutaisi
        ctr = getattr(self.airfield.ctr, "name", "") or ""
        if ctr.lower().endswith(" ctr"):
            known.add(ctr[:-4].strip().lower())
        if name not in known:
            return None
        return label, (self.airfield.ctr.center_lat, self.airfield.ctr.center_lon)

    def _inbound_reply(self, callsign: str, pilot: PilotState,
                       track: AircraftTrack | None, low: str = "") -> str:
        """Distance-aware inbound clearance (outside vs inside the CTR)."""
        if track is None:
            return self._say("report_runway_in_sight", callsign)
        if track.inside:
            return self._say("inbound_inside", callsign, position=track.relative)
        gate = pilot.entry_gate or self._pick_entry_gate(track, low)
        return self._say("inbound_via_gate", callsign, gate=gate)

    def _pick_exit_gate(self, traffic: list | None = None) -> str:
        """Departure exit point.

        Candidates are the gates that are not the arrival entry (see
        `Airfield.departure_gates`). With other traffic, take the one that is
        most free (fewest aircraft near it); otherwise rotate through them, so a
        pilot practises different exits instead of always flying the straight-out
        one. Falls back to the first configured gate.
        """
        if self.airfield is not None:
            candidates = self.airfield.departure_gates(self.runway)
            if candidates:
                if traffic:
                    return min(candidates,
                               key=lambda g: self._traffic_near_gate(g, traffic))
                gate = candidates[self._exit_rotation % len(candidates)]
                self._exit_rotation += 1
                return gate
        gate = self.gates[0] if self.gates else "North"
        return gate

    @staticmethod
    def _exit_label(gate: str) -> str:
        """Gate name as spoken in a clearance: "West" -> "Exit West"."""
        return gate if gate.lower().startswith("exit ") else f"Exit {gate}"

    def _traffic_near_gate(self, gate: str, traffic: list,
                           radius_nm: float = 5.0) -> int:
        """How many live units are within `radius_nm` of `gate` (lower = freer)."""
        if self.airfield is None:
            return 0
        target = self.airfield.gates.get(gate.split()[-1])
        if target is None:
            return 0
        count = 0
        for unit in traffic:
            _, distance = bearing_distance(unit.lat, unit.lon,
                                           target[0], target[1])
            if distance <= radius_nm:
                count += 1
        return count

    def _join_turn(self, track: AircraftTrack | None, heading: float) -> str:
        """Turn direction for the Control join: "left"/"right"/"".

        Computed from the aircraft's current heading to the join heading, so a
        pilot already pointing at the gate gets "fly heading X" (no turn) rather
        than a canned "turn right". Without a live heading we fall back to
        "right" (the historical default).
        """
        if track is None or self.airfield is None:
            return "right"
        return self.airfield.turn_to_heading(track.heading, heading)

    def _at_pattern_alt(self, track: AircraftTrack | None) -> bool:
        """True if the aircraft is at/below pattern altitude (or we cannot tell).

        Control holds the Tower handoff until the arrival has actually descended
        to 1500 ft. Without a live track we trust the pilot (offline tests and
        a missing radar still hand off on the readback).
        """
        if track is None:
            return True
        return track.alt_ft <= PATTERN_ALT_FT + PATTERN_ALT_SLOP_FT

    def _lined_up(self, track: AircraftTrack | None) -> bool:
        """True if the aircraft is lined up on the runway (or we cannot tell).

        Without a live track we cannot verify, so we trust the pilot (offline
        behaviour unchanged). With a track, the takeoff clearance waits until
        the aircraft is at the threshold, pointing down the runway, stationary.
        """
        if track is None or self.airfield is None:
            return True
        return self.airfield.is_lined_up(
            track.lat, track.lon, track.heading, self.runway,
            speed_kt=getattr(track, "speed_kt", None))

    def _takeoff_turnout(self, pilot: PilotState) -> str:
        """The turnout clause for the takeoff clearance: "left"/"right"/"straight".

        Real ATC (per MA): a **left** turnout is the default and is not spoken;
        a **right** turnout is called. A departure that flies the runway heading
        to its exit is told "fly runway heading".
        """
        if self.airfield is None:
            return "straight"
        gate = pilot.exit_gate or self._pick_exit_gate()
        turn = self.airfield.exit_turn(gate, self.runway)
        if turn == "right":
            return "right"
        if turn == "left":
            return "left"
        return "straight"

    def _takeoff_clearance(self, callsign: str, pilot: PilotState) -> str:
        """The takeoff clearance, with the turnout following the departure exit.

        Master Arms: "<callsign>, right turnout, cleared for take-off runway 25".
        Only a right turnout is spoken — a left turnout is the default and gets
        no clause; a straight-out departure is told "fly runway heading".
        """
        # A pilot can land, taxi, and depart again in the same DCS spawn. The
        # Tower->Control handoff is one-shot per departure, not per spawn.
        pilot.departure_handoff_announced = False
        pilot.departure_previous_range_nm = None
        pilot.departure_previous_bearing_deg = None
        pilot.control_checkin_received = False
        pilot.climb_issued = False
        turnout = self._takeoff_turnout(pilot)
        if turnout == "right":
            return self._say("lineup_readback", callsign, turnout=turnout)
        if turnout == "straight":
            return self._say("lineup_readback_straight", callsign)
        return self._say("lineup_readback_left", callsign)

    def _inside_ctr(self, track: AircraftTrack | None) -> bool:
        """True if the aircraft is inside the CTR footprint below the ceiling.

        Used to catch a departure that checks in with Control too early: Tower
        owns the zone up to the ceiling, so Control must not clear them up
        through it. Without a live track we cannot tell, so we do not block.
        """
        if track is None or self.airfield is None:
            return False
        return self.airfield.ctr.contains(track.lat, track.lon, track.alt_ft)

    def _departure_gate(self, pilot: PilotState) -> str:
        """The departure exit gate to name (assigned, else the runway default)."""
        return pilot.exit_gate or self._pick_exit_gate()

    def _pick_entry_gate(self, track: AircraftTrack | None,
                         low: str = "", pilot: PilotState | None = None) -> str:
        """Arrival entry point.

        A gate the pilot **requested** (or was previously cleared) is honoured
        first, so an approved "request Entry North" survives the inbound call.
        Otherwise it is the gate that best sets up the landing: the CTR is
        joined from the **approach side**, so we suggest the gate aligned with
        the reciprocal of the landing heading (Kutaisi 25 -> Entry East, 07 ->
        Entry West) — the common-sense straight-in entry, matching the Master
        Arms practice. The pilot's reported *position* does not change it. Falls
        back to the nearest gate to the live position, then a default.
        """
        if pilot is not None and pilot.entry_gate:
            return pilot.entry_gate
        if self.airfield is not None:
            gate = self.airfield.best_entry_gate(self.runway)
            if gate:
                return f"Entry {gate}"
        if track is not None and self.gate_locator is not None:
            gate = self.gate_locator(track.lat, track.lon)
            if gate:
                return f"Entry {gate}"
        return "Entry East"

    def _requested_gate(self, low: str) -> str | None:
        """A gate explicitly requested ("request Entry North", "we'd like Entry
        West"), as an "Entry X" label, or None.

        Requires a request verb *and* a gate name, so a bare directional position
        report ("35 miles north") is not mistaken for a request.
        """
        if not re.search(r"\b(request(?:ing)?|like|prefer|would like|"
                         r"asking for)\b", low):
            return None
        return self._spoken_gate(low)

    def _spoken_gate(self, low: str) -> str | None:
        """Entry gate named in the transmission, e.g. 'north' -> 'Entry North'."""
        for gate in self.gates:
            if re.search(rf"\b{re.escape(gate.lower())}\b", low):
                return f"Entry {gate}"
        return None

    def _at_entry(self, track: AircraftTrack | None, gate: str) -> bool:
        """True if near the named gate and aligned with its radial entry arc.

        Used to cross-check an "at the entry" report before handing the flight
        to Tower. Without a live track we cannot tell, so we trust the pilot
        (offline behaviour unchanged).
        """
        if track is None or self.airfield is None:
            return True
        name = gate.split()[-1]  # "Entry West" -> "West"
        target = self.airfield.gates.get(name)
        if target is None:
            return True
        position = self.airfield.gate_arc_position(
            track.lat, track.lon, gate)
        return (position is not None
                and 0 <= position[2] - position[1] <= ENTRY_REPORT_RADIUS_NM
                and self.airfield.in_gate_arc_sector(
                    track.lat, track.lon, gate,
                    GATE_ARC_HALF_ANGLE_DEG))

    def _distance_to_gate(self, track: AircraftTrack | None,
                          gate: str) -> float | None:
        """Distance (NM) from the aircraft to `gate`, or None if unknown."""
        if track is None or self.airfield is None:
            return None
        name = gate.split()[-1]  # "Entry West" -> "West"
        target = self.airfield.gates.get(name)
        if target is None:
            return None
        _, distance = bearing_distance(track.lat, track.lon,
                                       target[0], target[1])
        return distance

    def _descend_reply(self, callsign: str, pilot: PilotState,
                       track: AircraftTrack | None) -> str:
        """The arrival descent, stepped by distance AND altitude.

        A real controller steps an arrival down rather than dropping it to the
        final altitude 35 miles out. Far from the entry -> an intermediate
        step-down (5000 ft); within `DESCENT_FINAL_NM` -> the final altitude
        (1500 ft). Without a live position we issue the final descent (the
        documented Master Arms example).

        Two gates keep the step-down honest (debriefs 2026-10-18):

        - **Distance**: outside `DESCENT_FINAL_NM` of the gate -> the 5000
          step-down first.
        - **Altitude**: inside the distance gate but still HIGH — above
          `DESCENT_STEPDOWN_MAX_FT` — the straight-line gate distance lies
          (the pilot may be *abeam* the gate, not inbound to it: debrief
          21:17:59, checked in 6.5 NM north at Angels 14, 11 NM from gate
          West, and got the final descent with no step-down at all). Too
          high for the final -> the 5000 step-down first.

        In both cases the pilot's readback is remembered
        (`descend_readback`), so the monitors issue the final descent once
        they are actually low and close. The Tower handoff is a separate
        horizontal CTR-boundary event.
        """
        gate = pilot.entry_gate or self._pick_entry_gate(track, None, pilot)
        distance = self._distance_to_gate(track, gate)
        too_far = (distance is not None and distance > DESCENT_FINAL_NM)
        too_high = (track is not None
                    and track.alt_ft > DESCENT_STEPDOWN_MAX_FT)
        # Never clear an aircraft to descend to an altitude it has already
        # reached. Treat the 5000 ft step as satisfied when the live track is
        # already at or below it, while leaving the final descent armed for
        # the entry area.
        if (track is not None
                and track.alt_ft <= DESCENT_INTERMEDIATE_FT
                and (too_far or too_high)):
            pilot.descend_intermediate = True
            pilot.descend_readback = True
            if track.alt_ft <= PATTERN_ALT_FT:
                # The final target is already met too; don't issue a climb or
                # wait for a descent readback that cannot be required.
                pilot.descend_issued = True
            return ""
        if too_far or too_high:
            if pilot.descend_intermediate:
                # Already stepped down; this is the readback. Stay silent — the
                # next instruction (the final descent) follows once closer and
                # lower, so the controller does not acknowledge (see
                # check_arrival_descent).
                pilot.descend_readback = True
                return ""
            pilot.descend_intermediate = True
            return self._say("control_descend_step", callsign,
                             altitude=f"{DESCENT_INTERMEDIATE_FT} feet")
        if (track is not None
                and track.alt_ft > DESCENT_INTERMEDIATE_FT):
            # Inside the distance gate, below the step-down ceiling, but
            # still above the step-down altitude itself: the descent to
            # 5000 is in progress. Stay silent — the monitor issues the
            # final descent once 5000 is reached. The pilot HAS
            # acknowledged the descent plan, so remember the readback; the
            # entry-arc monitor can hand off without another call.
            pilot.descend_readback = True
            return ""
        if track is not None and track.alt_ft <= PATTERN_ALT_FT:
            pilot.descend_issued = True
            pilot.descend_readback = True
            return ""
        pilot.descend_issued = True
        # `descend_readback` tracks the most recent clearance. If this is the
        # final 1500 ft clearance, a readback of the earlier 5000 ft step must
        # not satisfy the Tower handoff prerequisite.
        pilot.descend_readback = False
        return self._say("control_descend", callsign)

    def check_departure_handoff(self, callsign: str,
                                track: AircraftTrack | None) -> str | None:
        """Push a departure to Control after it crosses an outbound gate arc.

        Tower initiates the Control handoff when radar shows the flight passing
        an outbound exit arc. A pilot report is not needed to trigger it. If
        the pilot passes a different configured departure gate, hand off there
        and coach the deviation.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.TAKEOFF or pilot.departure_handoff_announced
                or track is None or self.airfield is None):
            return None
        current_bearing, current_range = bearing_distance(
            self.airfield.ctr.center_lat, self.airfield.ctr.center_lon,
            track.lat, track.lon)
        previous_range = pilot.departure_previous_range_nm
        previous_bearing = pilot.departure_previous_bearing_deg
        pilot.departure_previous_range_nm = current_range
        pilot.departure_previous_bearing_deg = current_bearing
        if self.airfield.is_over_runway(track.lat, track.lon):
            return None
        gates = self.airfield.departure_gates(self.runway)
        passed_gate = next((gate for gate in gates
                            if (self.airfield.gate_arc_crossed_outbound(
                                    previous_range, previous_bearing,
                                    track.lat, track.lon, gate)
                                if (previous_range is not None
                                    and previous_bearing is not None) else
                                self.airfield.gate_arc_passed_outbound(
                                    track.lat, track.lon, gate))), None)
        if passed_gate is None:
            return None
        pilot.departure_handoff_announced = True
        # Crossing the departure exit makes the flight physically airborne,
        # even though its checked-in radio agency remains Tower until it calls
        # Control. These are separate pieces of state.
        pilot.phase = Phase.AIRBORNE
        pilot.pending_handoff_to = Controller.CONTROL.value
        note = self._exit_deviation_note(pilot, passed_gate)
        pilot.departure_previous_range_nm = None
        pilot.departure_previous_bearing_deg = None
        return note + self._say("contact_control", callsign)

    def _exit_deviation_note(self, pilot: PilotState,
                             passed_gate: str | AircraftTrack | None) -> str:
        """Coach only when the aircraft actually leaves through another gate."""
        if isinstance(passed_gate, AircraftTrack):
            track = passed_gate
            passed_gate = None
            if self.airfield is not None:
                passed_gate = next((gate for gate in
                                    self.airfield.departure_gates(self.runway)
                                    if self.airfield.gate_arc_passed_outbound(
                                        track.lat, track.lon, gate)), None)
        if passed_gate is None:
            return ""
        if not pilot.exit_gate or passed_gate == pilot.exit_gate:
            return ""
        return self._say("exit_deviation", pilot.callsign,
                         assigned_gate=self._exit_label(pilot.exit_gate)) + " "

    def check_departure_climb(self, callsign: str,
                              track: AircraftTrack | None) -> str | None:
        """Climb a departure once it is clear of the control zone.

        Control must have received the pilot's check-in before issuing a climb.
        If the pilot checks in inside the CTR, Control tells them to remain
        below the ceiling; the climb then goes out automatically once clear.
        Fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.AIRBORNE or not pilot.control_checkin_received
                or pilot.climb_issued or track is None):
            return None
        if self._inside_ctr(track):
            return None
        pilot.climb_issued = True
        return self._say("control_climb", callsign)

    def check_stingray_handoff(self, callsign: str,
                               track: AircraftTrack | None) -> str | None:
        """Push a climbed departure to Stingray once they reach Angels 15.

        Master Arms SOP (kneeboard page 1): after the climb to Angels 15,
        Control hands the flight to Stingray on channel 3. Stingray is now a
        real controller (check-in, bullseye picture, RTB); Control stays quiet
        until the inbound call. Fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.AIRBORNE or not pilot.control_checkin_received
                or not pilot.climb_issued
                or pilot.stingray_handoff_announced or track is None):
            return None
        if self._inside_ctr(track):
            return None
        if track.alt_ft < ANGELS_HANDOFF_FT - ANGELS_HANDOFF_SLOP_FT:
            return None
        pilot.stingray_handoff_announced = True
        pilot.pending_handoff_to = Controller.STINGRAY.value
        return self._say("contact_stingray", callsign)

    def check_stingray_rtb_handoff(self, callsign: str,
                                   track: AircraftTrack | None) -> str | None:
        """One delayed Stingray-to-Control push after an RTB call.

        The MA SOP gives a broad 25–80 NM window. A single midpoint trigger
        avoids repeated pushes when a flight orbits. Wait briefly after RTB so
        the acknowledgement and frequency change are not back-to-back; pilots
        can request Control at any time if they missed the push.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.STINGRAY or not pilot.rtb_requested
                or pilot.rtb_control_handoff_announced
                or pilot.pending_handoff_to == Controller.CONTROL.value
                or track is None):
            return None
        if track.distance_nm > RTB_CONTROL_HANDOFF_NM:
            return None
        if time.monotonic() < pilot.rtb_control_handoff_not_before:
            return None
        pilot.rtb_control_handoff_announced = True
        pilot.pending_handoff_to = Controller.CONTROL.value
        return self._say("stingray_contact_control", callsign)

    def check_arrival_tower_handoff(self, callsign: str,
                                    track: AircraftTrack | None) -> str | None:
        """Push arrivals at their entry arc; use the CTR as a one-shot fallback.

        This is a spatial handoff: neither the final descent transmission nor
        its readback can block the assigned entry-arc trigger. If the aircraft
        misses the arc, entering the CTR is the one-shot fallback so a missed
        descent dialog cannot leave the flight stuck on Control.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.INBOUND
                or pilot.checked_in_at != Controller.CONTROL.value
                or pilot.tower_handoff_announced
                or pilot.pending_handoff_to == Controller.TOWER.value
                or track is None or self.airfield is None):
            return None
        gate = pilot.entry_gate or self._pick_entry_gate(track, None, pilot)
        position = self.airfield.gate_arc_position(track.lat, track.lon, gate)
        previous_range = pilot.arrival_previous_range_nm
        previous_bearing = pilot.arrival_previous_bearing_deg
        pilot.arrival_previous_range_nm = position[1] if position else None
        pilot.arrival_previous_bearing_deg = position[0] if position else None

        crossed_arc = bool(
            position
            and self.airfield.in_gate_arc_sector(
                track.lat, track.lon, gate, GATE_ARC_HALF_ANGLE_DEG)
            and (position[1] <= position[2]
                 or (previous_range is not None
                     and previous_bearing is not None
                     and self.airfield.gate_arc_crossed_inbound(
                         previous_range, previous_bearing,
                         track.lat, track.lon, gate))))
        ctr_fallback = self.airfield.ctr.contains_horizontal(
            track.lat, track.lon)
        if not crossed_arc and not ctr_fallback:
            return None
        pilot.tower_handoff_announced = True
        pilot.pending_handoff_to = Controller.TOWER.value
        pilot.arrival_previous_range_nm = None
        pilot.arrival_previous_bearing_deg = None
        return self._say("contact_tower_from_control", callsign)

    def check_arrival_descent(self, callsign: str,
                              track: AircraftTrack | None) -> str | None:
        """Issue the final arrival descent once close enough AND low enough.

        After the intermediate step-down (issued far out or high), Control
        calls the final "descend to 1500 feet" when the aircraft reaches the
        entry area — so the pilot is not left at the intermediate altitude.
        Fires once. Both gates matter (debriefs 2026-10-18): an aircraft
        inside the distance gate but still above 5000 ft has not finished
        the step-down (20:46:38), and one abeam the gate at 14 000 ft was
        never stepped down at all (21:17:59).
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.INBOUND
                or pilot.pending_handoff_to == Controller.TOWER.value
                or pilot.descend_issued
                or not (pilot.descend_intermediate
                        or pilot.descend_readback)):
            return None
        gate = pilot.entry_gate or self._pick_entry_gate(track, None, pilot)
        distance = self._distance_to_gate(track, gate)
        if distance is not None and distance <= DESCENT_FINAL_NM:
            if track is not None and track.alt_ft > DESCENT_INTERMEDIATE_FT:
                return None
            if track is not None and track.alt_ft <= PATTERN_ALT_FT:
                # Already at/below the final target: mark it satisfied without
                # transmitting a descent clearance that would imply a climb.
                pilot.descend_issued = True
                pilot.descend_readback = True
                return None
            pilot.descend_issued = True
            # Require a readback of this final clearance; a prior 5000 ft
            # readback does not complete the arrival sequence.
            pilot.descend_readback = False
            return self._say("control_descend", callsign)
        return None

    def check_descent_handoff(self, callsign: str,
                              track: AircraftTrack | None) -> str | None:
        """Compatibility entry point for the CTR-driven arrival handoff."""
        return self.check_arrival_tower_handoff(callsign, track)

    def _spoken_parking(self, low: str) -> str | None:
        """Parking area named in the transmission, e.g. 'ramp north'."""
        if self.airfield is None:
            return None
        for area in self.airfield.parking_areas:
            if re.search(rf"\b{re.escape(area.lower())}\b", low):
                return area
        return None

    def _help(self, callsign: str, pilot: PilotState,
              controller: Controller | None = None,
              track: AircraftTrack | None = None) -> str:
        """A short, state-aware hint for a pilot who is unsure what to do next.

        Triggered by "<callsign> help". The hint depends on the pilot's current
        phase, so it always tells them the *next* call to make. `controller` is
        the agency they are talking to, used to pick the right Phase.AIRBORNE
        hint: a departure who checks in with Control is already on Control's
        frequency, so "contact Control on channel 8" would send them in a
        circle (debrief 2026-10-22).

        `track` is the pilot's live position, used by the Stingray hint to state
        the *actual* bullseye bearing/range — so a pilot can check their numbers
        before calling them out.
        """
        key = {
            Phase.IDLE: "help_idle",
            Phase.CLEARANCE: "help_clearance",
            Phase.TAXI: "help_taxi",
            Phase.HOLDING: "help_holding",
            Phase.LINEUP: "help_lineup",
            Phase.TAKEOFF: "help_departure",
            Phase.AIRBORNE: "help_airborne",
            Phase.STINGRAY: "help_stingray",
            Phase.INBOUND: "help_inbound",
            Phase.LANDING: "help_landing",
        }.get(pilot.phase, "help_idle")
        if pilot.phase == Phase.AIRBORNE and controller == Controller.CONTROL:
            # Already with Control: the SOP next step is the Stingray push once
            # at Angels 15, or the inbound call when they turn back.
            key = "help_airborne_on_control"
        elif pilot.phase == Phase.IDLE and controller == Controller.STINGRAY:
            # An air-start on Stingray: the idle hint ("contact Ground for
            # clearance and taxi") is actively wrong here, so give the tactical
            # leg instead.
            key = "help_stingray"
        fields: dict[str, object] = {"gate": pilot.exit_gate or "Exit East"}
        if key == "help_stingray":
            # State the real bullseye so the pilot can check their numbers
            # before calling them out. Without a bullseye (or a live position)
            # fall back to the generic example, so the hint never reads
            # "you are —".
            bullseye = self._bullseye_hint(track)
            if bullseye:
                fields["bullseye"] = bullseye
            else:
                key = "help_stingray_nobullseye"
        hint = self._say(key, callsign, **fields)
        # Always remind the pilot of the escape hatches, so they can never get
        # stuck in a wrong state during training.
        return f"{hint} {self._say('help_escape', callsign)}"

    def _bullseye_hint(self, track: AircraftTrack | None) -> str:
        """The pilot's current bullseye position, for the help hint.

        "bullseye 065 for 40, angels 15" — the exact call they should make.
        Empty when the mission has no bullseye or we have no live position, so
        the hint simply omits it rather than stating a wrong number.
        """
        if self.bullseye is None or track is None:
            return ""
        bearing, distance = bearing_distance(
            self.bullseye[0], self.bullseye[1], track.lat, track.lon)
        return (f"bullseye {self._cockpit_bearing(bearing):03.0f} for "
                f"{distance:.0f}, "
                f"angels {track.alt_ft / 1000.0:.0f}")

    def _directions(self, callsign: str, low: str,
                    track: AircraftTrack | None,
                    controller: Controller,
                    pilot: PilotState | None = None) -> str:
        """Bearing/distance to a named gate (or the field) from the pilot's
        live position. A trainer aid to help find the CTR entry/exit points."""
        agency = self._agency(controller)
        if track is None or self.airfield is None:
            return self._say("directions_unknown", callsign, agency=agency)
        # Which gate? Prefer one named in the call, else the gate the pilot is
        # cleared to (or the best entry for the runway) — so a request for
        # "directions to the field" points at the entry that sets up the landing.
        gate = self._spoken_gate(low)
        if gate is None and pilot is not None and pilot.entry_gate:
            gate = pilot.entry_gate
        if gate is None:
            gate = self._pick_entry_gate(track, low, pilot)
        if gate is None:
            # no gates configured: give directions to the field itself
            bearing, distance = bearing_distance(
                track.lat, track.lon, self.airfield.ctr.center_lat,
                self.airfield.ctr.center_lon)
            return self._say("directions_field", callsign, agency=agency,
                             airfield=self.airfield.name,
                             bearing=f"{bearing:03.0f}", distance=f"{distance:.0f}")
        name = gate.split()[-1]  # "Entry East" -> "East"
        target = self.airfield.gates.get(name)
        if target is None:
            return self._say("directions_unknown", callsign, agency=agency)
        bearing, distance = bearing_distance(track.lat, track.lon,
                                             target[0], target[1])
        return self._say("directions_gate", callsign, agency=agency, gate=gate,
                         bearing=f"{bearing:03.0f}", distance=f"{distance:.0f}")

    def _vectors(self, callsign: str, low: str,
                 track: AircraftTrack | None,
                 controller: Controller) -> str:
        """An approach vector: "fly heading X, vectors for runway Y".

        Real ATC phraseology (e.g. "090 for 25"). The heading is the bearing to
        a point on the extended centreline, so the pilot can intercept the
        approach. A trainer aid.
        """
        agency = self._agency(controller)
        if track is None or self.airfield is None:
            return self._say("directions_unknown", callsign, agency=agency)
        # Runway named in the call (e.g. "vectors for runway 25"), else active.
        runway = self.runway
        match = re.search(r"\b(?:runway\s*)?(\d{2})\b", low)
        if match and match.group(1) in self.airfield.runways:
            runway = match.group(1)
        heading = self.airfield.vector_heading(track.lat, track.lon, runway)
        if heading is None:
            return self._say("directions_unknown", callsign, agency=agency)
        pilot = self._pilot(callsign)
        pilot.vector_heading = f"{self._cockpit_bearing(heading):03.0f}"
        pilot.vector_runway = runway
        return self._say("vectors", callsign, agency=agency,
                          heading=pilot.vector_heading,
                          vector_runway=runway)

    def working_controller(self, callsign: str) -> Controller:
        """Last agency this pilot talked to; Tower if they have never checked in.

        Unsolicited calls (CTR warning, go-around, TopGun sting) must go on that
        working frequency: this trainer has no Guard, and a Tower-only call
        never reaches a pilot still on Control.
        """
        last = (self._pilot(callsign).last_controller or "").lower()
        for ctrl in Controller:
            if ctrl.value == last:
                return ctrl
        return Controller.TOWER

    def on_ctr_event(self, callsign: str, event: CtrEvent,
                     track: AircraftTrack) -> str | None:
        """React to a CTR boundary crossing. Returns a warning to broadcast,
        or None if no action is needed."""
        pilot = self._pilot(callsign)
        if event == CtrEvent.ENTERED and not pilot.cleared_inbound:
            agency = self._agency(self.working_controller(callsign))
            return self._say("ctr_warning", callsign, agency=agency)
        return None

    def check_landing_clearance(self, callsign: str,
                                track: AircraftTrack | None,
                                traffic: list | None = None,
                                speaker: str = "") -> str | None:
        """Issue the landing clearance by radar, unprompted, once established.

        The overhead break (and the pilot's pattern calls) are STATUS
        information, not a clearance request: Tower watches the aircraft and
        clears it to land once it has rolled out of the turn and is
        established on final (inside the tight final cone, outside the pattern
        turn slices — see `Airfield.is_established_final`). This mirrors how
        the takeoff clearance works (`check_lineup`): the pilot's calls arm
        the flow, the radar delivers the clearance.

        Fires once per approach (re-arms on a go-around / touch-and-go, when
        the phase returns to Inbound). Without a live track we do nothing —
        the pilot's "on final" call remains the fallback path.

        The phase gate accepts both arrival phases: INBOUND (straight-in or
        radar arrival) and LANDING (post-OHB — the break clearance puts the
        pilot in Landing while they fly the pattern, but `cleared_landing` is
        still false until this issues the actual clearance).

        **Only for the OHB flow** (`pilot.break_cleared`): the radar may clear
        an aircraft that Tower has sent into the pattern once it rolls out.
        A straight-in arrival also sits on the extended centreline — often
        already inside the cone at the handoff (debrief 2026-10-18 19:21:08:
        "cleared to land" fired 13 s after the handoff, 12 NM out at Entry
        West, before the break) — but that arrival has not been given the
        pattern, so its clearance still comes from the SOP "on final" call.

        And the radar must have **seen the turn**: the clearance requires
        `pattern_flown` — the aircraft was observed inside a pattern turn
        slice since the break clearance. Being pointed at the runway is not
        enough (debrief 2026-10-18 20:49:55: the RWIS report at 8 NM put the
        aircraft on the centreline, and the clearance fired 1 s later, before
        the break). The slices are the radar evidence that the OHB happened.
        """
        pilot = self._pilot(callsign)
        if (track is None or self.airfield is None
                or pilot.phase not in (Phase.INBOUND, Phase.LANDING)
                or not pilot.break_cleared or pilot.cleared_landing):
            return None
        # Radar observation: note slice passage while the aircraft flies the
        # pattern (checked every tick, before the established-final test —
        # the slices and the final cone do not overlap, so a single tick
        # cannot be both).
        if self.airfield.in_pattern_turn(track.lat, track.lon, self.runway):
            pilot.pattern_flown = True
            return None
        if not pilot.pattern_flown:
            return None
        if not self.airfield.is_established_final(track.lat, track.lon,
                                                  track.heading, self.runway):
            return None
        # Timing window: give the pilot the chance to SAY "on final" first
        # (the SOP call). The radar clearance is the backstop, not the
        # opener: it fires once the aircraft is inside RADAR_CLEAR_NM —
        # close enough that staying silent any longer would leave them
        # un-cleared on short final — but well before that they had room
        # to make the call and get the clearance from the voice path.
        if track.distance_nm > RADAR_CLEAR_NM:
            return None
        if self._runway_busy(traffic, exclude=speaker):
            # Occupied: hold the clearance (the go-around monitor in
            # check_final covers the "someone is on the runway" case; here we
            # simply wait until it clears, like check_runway_clear does).
            return None
        return self._issue_landing_clearance(callsign, pilot)

    def _issue_landing_clearance(self, callsign: str,
                                 pilot: PilotState) -> str:
        """Advance the pilot to Landing and speak the landing clearance.

        Shared by the radar path (`check_landing_clearance`) and the voice
        path ("on final"). Marks same-flight wingmen as covered: the Master
        Arms landing clearance is valid for the whole flight.
        """
        pilot.phase = Phase.LANDING
        pilot.cleared_landing = True
        pilot.awaiting_landing = False
        pilot.landing_ready_announced = False
        for wing in self._flight_members(callsign):
            wing_state = self.pilots.get(wing)
            if wing_state is not None and wing_state.phase in (
                    Phase.INBOUND, Phase.LANDING):
                wing_state.cleared_landing = True
        return self._say("cleared_land", callsign,
                         flight=self._formation_prefix(pilot))

    def check_final(self, callsign: str, on_final: bool,
                    runway_occupied: bool,
                    traffic: list | None = None) -> str | None:
        """Runway-occupancy check for an aircraft on final approach.

        Returns a go-around call, or None. Only fires once per approach (until
        the aircraft is no longer on final).
        """
        pilot = self._pilot(callsign)
        if not on_final:
            pilot.go_around_issued = False
            return None
        if runway_occupied and not pilot.go_around_issued:
            pilot.go_around_issued = True
            pilot.phase = Phase.INBOUND
            pilot.cleared_landing = False
            pilot.pattern_flown = False
            pilot.landing_handoff_announced = False
            key = ("go_around_traffic" if self._traffic_on_runway(traffic)
                   else "go_around")
            return self._say(key, callsign)
        return None

    def _traffic_on_runway(self, traffic: list | None = None) -> bool:
        """True if any tracked unit is actually on the runway corridor.

        Uses the same geometry as sequencing (`runway_occupied`), so the
        go-around reason matches what blocked the landing. `traffic` is the
        live unit list from the caller; empty means unknown, and the generic
        "runway is occupied" wording is used.
        """
        if self.airfield is None or not traffic:
            return False
        return self.airfield.runway_occupied(traffic)

    def check_altitude(self, callsign: str, track: AircraftTrack) -> str | None:
        """Warn a pilot who has climbed above the CTR ceiling while still in a
        Tower phase (i.e. has not been handed to Control).

        Fires once per excursion (re-arms when back below the ceiling). Pilots
        already talking to Control (Airborne/Inbound/Landing) are exempt — they
        are cleared above the CTR.
        """
        pilot = self._pilot(callsign)
        if self.airfield is None:
            return None
        ctr = self.airfield.ctr
        above = (track.alt_ft > ctr.ceiling_ft_msl
                 and ctr.contains_horizontal(track.lat, track.lon))
        if not above:
            pilot.altitude_warned = False
            return None
        # Only warn pilots still under Tower/Ground control (not handed off).
        if pilot.phase in (Phase.AIRBORNE, Phase.INBOUND, Phase.LANDING):
            return None
        if pilot.altitude_warned:
            return None
        pilot.altitude_warned = True
        return self._say("altitude_bust", callsign)

    def check_incursion(self, callsign: str, on_runway: bool) -> str | None:
        """Warn a pilot who is on the runway without a takeoff/landing clearance.

        Fires once per incursion (re-arms when off the runway), but only after
        the aircraft has stayed on the runway across several polls: a pilot
        taxiing forward to line up after the "contact Tower" handoff often
        clips the runway pavement while still in the Holding phase, and a real
        tower does not yell "vacate immediately" at them for that.
        A pilot in the Departure or Landing phase is legitimately on the runway. So is a pilot
        in **Lineup** — "line up and wait" *explicitly* puts them on the runway,
        so warning them to "vacate immediately" would be wrong (and did, before
        this was added). So is a pilot in **Holding** — the Ground->Tower
        handoff fires while the pilot is still at the holding point, and
        taxiing forward from there to line up clips the runway pavement
        without ever being an incursion (debrief 2026-10-18 12:51:21).
        """
        pilot = self._pilot(callsign)
        if not on_runway:
            pilot.incursion_warned = False
            pilot.incursion_streak = 0
            return None
        # A pilot in Departure or Landing is legitimately on the runway. So is
        # one in Lineup ("line up and wait" puts them there) and one in
        # **Airborne** — a departing aircraft climbing out over the runway
        # corridor is not an incursion (this fired a false "vacate immediately"
        # right after takeoff, once the pilot called "airborne").
        # A pilot who was CLEARED TO LAND is also legitimate: the Tower->Ground
        # handoff fires at touchdown (phase becomes Taxi while still rolling
        # out), which otherwise looked like an uncleared incursion. Scoped to
        # Landing/Taxi so it does not persist into a later departure.
        if pilot.phase in (Phase.HOLDING, Phase.LINEUP, Phase.TAKEOFF,
                           Phase.AIRBORNE, Phase.LANDING) or (
                pilot.cleared_landing
                and pilot.phase in (Phase.LANDING, Phase.TAXI)):
            return None
        if pilot.incursion_warned:
            return None
        pilot.incursion_streak += 1
        if pilot.incursion_streak < INCURSION_POLLS:
            return None
        pilot.incursion_warned = True
        return self._say("runway_incursion", callsign)

    def check_runway_clear(self, callsign: str,
                           runway_occupied: bool) -> str | None:
        """Call a held pilot back, unprompted, once the runway clears.

        Real ATC does not leave a pilot hanging after "hold short, runway
        occupied" — it calls them back when able. This issues the *real* next
        clearance: "line up and wait runway X" for a departure, or "cleared to
        land" for an arrival. (Controllers deliver the clearance itself; there
        is no standard "the runway is now clear" phrase.) Fires once, so an idle
        pilot is not spammed, and re-arms when they are held again.
        """
        pilot = self._pilot(callsign)
        if runway_occupied:
            return None
        if (pilot.awaiting_takeoff and not pilot.takeoff_ready_announced
                and pilot.phase in (Phase.HOLDING, Phase.LINEUP)):
            pilot.takeoff_ready_announced = True
            pilot.awaiting_takeoff = False
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        if (pilot.awaiting_landing and not pilot.landing_ready_announced
                and pilot.phase == Phase.INBOUND):
            pilot.landing_ready_announced = True
            pilot.awaiting_landing = False
            pilot.phase = Phase.LANDING
            pilot.cleared_landing = True
            return self._say("cleared_land", callsign,
                             flight=self._formation_prefix(pilot))
        return None

    def check_lineup(self, callsign: str,
                     track: AircraftTrack | None) -> str | None:
        """Issue the takeoff clearance once the pilot is actually lined up.

        After "line up and wait" the pilot reads it back, but the takeoff
        clearance must not follow until they have taxied onto the runway and are
        lined up (at the threshold, pointing down the runway, stationary). The
        monitor calls this each tick; it fires once, then the phase is Departure.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.LINEUP or not pilot.lineup_readback
                or track is None or self.airfield is None):
            return None
        if not self.airfield.is_lined_up(
                track.lat, track.lon, track.heading, self.runway,
                speed_kt=getattr(track, "speed_kt", None)):
            return None
        pilot.lineup_readback = False
        pilot.phase = Phase.TAKEOFF
        return self._takeoff_clearance(callsign, pilot)

    def check_landing_handoff(self, callsign: str, on_runway: bool) -> str | None:
        """Push a landed flight to Ground, unprompted, once it is rolling out.

        Master Arms SOP (Mission Procedures §9): "after landing, tower will push
        you to the Ground channel" — the pilot does not report "vacated" to
        Tower. The handoff happens once the aircraft is **on the ground and
        rolling out** (the caller passes `on_runway=True` only then, so a
        touch-and-go still fast on the runway is not pushed), so the pilot is
        already on Ground by the time they are clear. Fires once per landing;
        re-arms on the next approach (go-around / touch-and-go).
        """
        pilot = self._pilot(callsign)
        if not on_runway:
            # On approach again (go-around / touch-and-go): re-arm for the next
            # landing. After a handoff the phase is Taxi, so this does not
            # re-arm while the aircraft is still rolling out.
            if pilot.phase in (Phase.INBOUND, Phase.LANDING):
                pilot.landing_handoff_announced = False
            return None
        if (pilot.phase == Phase.LANDING and pilot.cleared_landing
                and not pilot.landing_handoff_announced):
            pilot.landing_handoff_announced = True
            pilot.phase = Phase.TAXI
            pilot.pending_handoff_to = Controller.GROUND.value
            return self._say("contact_ground", callsign)
        return None
