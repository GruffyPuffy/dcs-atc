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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import topgun
from atis import wind_speed_kt
from callsigns import CallsignRegistry, flight_name
from ctr import AircraftTrack, CtrEvent
from airspace import bearing_distance
from state_client import canonical_callsign, slot_callsign

TOWER = "Kutaisi Tower"
RUNWAY = "25"
DEFAULT_PHRASEOLOGY = Path(__file__).with_name("phraseology.json")

# How close (NM) a pilot must be to a named entry gate for an "at the entry"
# report to be accepted. Beyond this, Control challenges the position instead of
# handing the flight to Tower (a pilot who is not actually at the entry point
# should not be handed off).
ENTRY_REPORT_RADIUS_NM = 5.0
# How close a departure must be to its assigned exit gate when Tower hands it to
# Control; further away gets a trainer note (the handoff still happens). Kept
# small: the gate is a point on the CTR boundary, so a wide radius would accept
# almost any departure direction.
EXIT_REPORT_RADIUS_NM = 1.5

# Arrival descent is stepped by distance to the entry gate, like a real
# controller (not a single canned "descend to 1500" at 35 miles). Within
# `DESCENT_FINAL_NM` of the entry -> cleared down to the final altitude (1500
# ft); further out -> an intermediate step-down (5000 ft) first.
DESCENT_FINAL_NM = 15.0
DESCENT_INTERMEDIATE_FT = 5000
# Pattern altitude Control descends an arrival to, and the slop for "you are
# there" (altimeter / radar, not a hard deck).
PATTERN_ALT_FT = 1500
PATTERN_ALT_SLOP_FT = 200
# Departure climb target (Angels 15) and slop for the unprompted Stingray
# handoff. Control does not simulate AWACS, but MA SOP still pushes the flight
# to Stingray on channel 3 once they are there.
ANGELS_HANDOFF_FT = 15000
ANGELS_HANDOFF_SLOP_FT = 500

# How many consecutive polls with the aircraft on the runway before the
# "vacate immediately" incursion warning fires (~2 s poll cadence -> 10 s).
# A pilot creeping forward to line up right after the Ground->Tower handoff
# clips the pavement briefly; warning them then is noise, not safety.
INCURSION_POLLS = 5

# Spoken number words -> digits, for readback verification. STT writes what
# it hears: "runway seven", "QNH two nine nine six", "5,000 feet" (with a
# comma). The verifiers compare against digits, so normalise first.
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "niner": "9",
}


def _normalise_spoken_numbers(text: str) -> str:
    """STT-friendly number normalisation for readback matching.

    - number words -> digits ("runway seven" -> "runway 7")
    - digit groups with separators -> plain digits ("5,000" -> "5000",
      "1,500" -> "1500")
    - "two nine nine six" (a spoken digit *sequence*) -> "2996" so a QNH
      readback matches "2992"-style figures.
    """
    text = re.sub(r"\b(\d{1,3}),(\d{3})\b", r"\1\2", text)
    text = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b",
                  lambda m: _NUMBER_WORDS[m.group(1)], text)
    # A run of single digits separated by spaces is one spoken figure
    # ("two nine nine six" -> "2996"). Only merge runs of 2+ to avoid
    # fusing unrelated single digits.
    text = re.sub(r"\b(\d)(\s+\d){1,}\b",
                  lambda m: re.sub(r"\s+", "", m.group(0)), text)
    return text


def _stem(word: str) -> str:
    """A regex fragment matching a verb in its common spoken forms.

    STT transcribes inflections: "descending", "maintaining", "cleared".
    `\bword\b` misses them ("descend" does not match "descending"), so match
    the stem with optional suffixes instead.
    """
    return rf"\b{word}(?:ing|ed|s)?\b"


class Controller(str, Enum):
    """Which controlling agency is being addressed (see ATC.md §11)."""

    GROUND = "ground"
    TOWER = "tower"
    CONTROL = "control"


class Phase(str, Enum):
    """Per-aircraft flight phase (see ATC.md §5)."""

    IDLE = "Idle"
    CLEARANCE = "Clearance"
    TAXI = "Taxi"
    HOLDING = "Holding"
    LINEUP = "Lineup"
    DEPARTURE = "Departure"
    AIRBORNE = "Airborne"
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
    go_around_issued: bool = False
    descend_issued: bool = False  # Control has issued the descent to 1500 ft
    descend_intermediate: bool = False  # Control issued the far-out step-down
    climb_issued: bool = False  # Control has issued the departure climb
    # Control has pushed the departure to Stingray (AWACS). Fires once; we do
    # not simulate channel 3, but the SOP call still goes out on Control.
    stingray_handoff_announced: bool = False
    altitude_warned: bool = False  # warned about busting the CTR ceiling
    incursion_warned: bool = False  # warned about being on the runway uncleared
    incursion_streak: int = 0  # consecutive on-runway polls (debounce)
    exit_gate: str = ""  # assigned departure exit point
    entry_gate: str = ""  # assigned arrival entry point
    # Held because the runway was occupied; the bot calls the pilot back when it
    # clears. `*_ready_announced` fires the unprompted "runway is clear" once.
    awaiting_takeoff: bool = False
    awaiting_landing: bool = False
    takeoff_ready_announced: bool = False
    landing_ready_announced: bool = False
    # The pilot has read back "line up and wait" but is not yet lined up on the
    # runway. The takeoff clearance waits until they are (see check_lineup).
    lineup_readback: bool = False
    # The pilot has read back "descend to 1500 feet" but is not yet there.
    # Control holds the Tower handoff until they are (see check_descent_handoff).
    descend_readback: bool = False
    # Tower has pushed a landed flight to Ground (Master Arms SOP: "after
    # landing, tower will push you to the Ground channel"). Fires once per
    # landing; re-arms on the next approach (go-around / touch-and-go).
    landing_handoff_announced: bool = False
    # Tower has pushed a departure to Control (MA SOP: "expect instructions to
    # switch to control" once airborne). Fires once per departure.
    departure_handoff_announced: bool = False
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
    readback_fails: int = 0  # consecutive incomplete readbacks of one clearance
    # Ground has read out the runway/QNH (or weather) info, so a later call
    # naming "in use"/QNH is a readback of it. Without this, any transmission
    # that merely mentions "in use" was answered "readback correct".
    info_issued: bool = False


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
                 gates: list[str] | None = None,
                 gate_locator=None, airfield=None):
        self.tower = tower
        self.runway = runway
        self.ground = ground
        self.control = control
        # Master Arms: an agency identifies itself by ROLE ("Tower", "Ground",
        # "Control"), not the full name ("Kutaisi Tower"). The full name is only
        # used by the pilot when initiating contact. Replies use the short name.
        self.tower_short = tower.split()[-1] if tower else "Tower"
        self.ground_short = ground.split()[-1] if ground else "Ground"
        self.control_short = control.split()[-1] if control else "Control"
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
        # TTS output volume, adjustable at runtime with "trainer, volume 0.3".
        # None = use the --tx-volume default.
        self.tx_volume: float | None = None
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
                                    self.control_short, "atis", "tower",
                                    "ground", "control", "approach",
                                    "departure", "center", "centre")}

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
            control=self.control_short, qnh=self.qnh, weather=self.weather,
            **overrides, **fields)

    def _agency(self, controller: Controller) -> str:
        """Short name of the agency handling a controller role."""
        return {Controller.GROUND: self.ground_short,
                Controller.CONTROL: self.control_short,
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
        return (self._say("position_challenge", callsign, agency=agency,
                          position=position)
                + " " + self._say("help_escape", callsign))

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
    # "one five hundred") can be compared with the numeric clearance.
    _WORD_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3",
                    "four": "4", "five": "5", "six": "6", "seven": "7",
                    "eight": "8", "nine": "9", "niner": "9"}

    @classmethod
    def _numbers(cls, text: str) -> set[str]:
        """Every number in `text`, with spoken digits folded in.

        "two niner niner two" -> {2992}; "0 7 0" -> {070}; "1500 feet" -> {1500}.
        Used to compare a readback against the clearance without depending on
        the filler words or the exact phrasing.
        """
        t = re.sub(r"[.,]", "", (text or "").lower())
        t = re.sub(r"\b(" + "|".join(cls._WORD_DIGITS) + r")\b",
                   lambda m: cls._WORD_DIGITS[m.group(1)], t)
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

        An incomplete readback is **not** called correct: it gets an honest
        "readback incomplete — {item} not read back, continue." so the pilot
        knows what to train on, and the caller still advances the phase, so a
        fumbled readback never traps them — they keep flying and fix it next
        time.
        """
        agency = self._agency(controller)
        item = self._readback_item(low, clearance)
        if item is None:
            pilot.readback_fails = 0
            return self._say(ok_key, callsign, agency=agency) if ok_key else ""
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
                return self._help(name, self._pilot(name))
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
        pilot.last_controller = controller.value
        self._note_formation(pilot, low)
        reply = self._dispatch(callsign, pilot, low, track, controller, traffic,
                               speaker)
        if reply:
            # Remember the last clearance so the pilot can ask for it again
            # ("say again") — a trainer aid for missed readbacks.
            pilot.last_reply = reply
        return reply
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
            return self._trainer(callsign, pilot, low, controller)

        # Trainer aid: "<callsign> request vectors [for runway 25]" gives an
        # approach vector ("fly heading X, vectors for runway Y"). Real ATC
        # phraseology — distinct from a bearing/distance request. A *request*
        # for a heading ("request heading to the field") is the same aid; a
        # readback ("turn right heading 150") is not (it has no request verb).
        if re.search(r"\bvectors?\b", low) or (
                re.search(r"\bheading\b", low)
                and re.search(r"\b(request(?:ing)?|like|prefer|would like|"
                              r"asking for|need|give me|what)\b", low)):
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
        if re.search(r"\b(cancel|cancelling|canceling|disregard|abort|aborting|"
                     r"scratch that|scratch|belay|cancel clearance)\b", low):
            return self._cancel(callsign, pilot, controller)
        if re.search(r"\b(say again|repeat|repeat last|say again please|"
                     r"repeat clearance)\b", low):
            return pilot.last_reply or self._say(
                "say_again", callsign, agency=self._agency(controller))

        if controller == Controller.GROUND:
            reply = self._handle_ground(callsign, pilot, low, track, traffic)
        elif controller == Controller.CONTROL:
            reply = self._handle_control(callsign, pilot, low, track)
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
            pilot.phase = Phase.DEPARTURE
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
        # Frequency-change acknowledgement ("Colt 1, channel 8, push"): the pilot
        # is switching, so we stay *silent* — a "roger" would only step on the
        # new frequency's first call.
        if re.search(r"\bchannel\s*\d\b|\bpush\b|\bswitching\b", low) \
                and not re.search(r"\b(request|clearance|taxi|inbound|requesting)\b",
                                  low):
            return ""
        if re.search(r"\bchecking (in|out)\b|\bwith you\b", low):
            return self._say("roger", callsign, agency=agency)
        if re.search(r"\b(reading back|roger|wilco|copy)\b", low):
            return self._say("roger", callsign, agency=agency)
        # bare callsign / unintelligible request: ask them to say again
        return self._say("say_again", callsign, agency=agency)

    def _trainer(self, callsign: str, pilot: PilotState, low: str,
                 controller: Controller) -> str:
        """Handle a "trainer, ..." command (trainer aid, any frequency).

        Currently: "trainer, echo on|off" toggles the in-game comms mirror for
        THIS pilot. Unknown trainer commands get a short usage hint.
        """
        agency = self._agency(controller)
        if re.search(r"\b(help|assist|what do i do|what now|remind me)\b", low):
            return self._help(callsign, pilot)
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
        return self._say("trainer_unknown", callsign, agency=agency)

    def mirror_enabled(self, callsign: str) -> bool:
        """True if this pilot asked for the in-game comms mirror."""
        pilot = self.pilots.get(callsign)
        return bool(pilot and pilot.mirror_logs)

    def _reset(self, callsign: str, pilot: PilotState,
               controller: Controller) -> str:
        """Full reset to the default state (trainer escape hatch)."""
        pilot.phase = Phase.IDLE
        pilot.cleared_inbound = False
        pilot.cleared_landing = False
        pilot.break_cleared = False
        pilot.go_around_issued = False
        pilot.descend_issued = False
        pilot.descend_intermediate = False
        pilot.climb_issued = False
        pilot.stingray_handoff_announced = False
        pilot.exit_gate = ""
        pilot.entry_gate = ""
        pilot.readback_fails = 0
        pilot.parking_cleared = False
        pilot.awaiting_takeoff = False
        pilot.awaiting_landing = False
        pilot.takeoff_ready_announced = False
        pilot.landing_ready_announced = False
        pilot.lineup_readback = False
        pilot.descend_readback = False
        pilot.landing_handoff_announced = False
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
            Phase.DEPARTURE: Phase.HOLDING,
            Phase.AIRBORNE: Phase.AIRBORNE,
            Phase.INBOUND: Phase.AIRBORNE,
            Phase.LANDING: Phase.INBOUND,
        }.get(pilot.phase, Phase.IDLE)
        pilot.phase = back
        pilot.cleared_landing = False
        pilot.break_cleared = False
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
        if re.search(r"\b(clearance|ready to copy|ifr)\b", low):
            pilot.phase = Phase.CLEARANCE
            gate = self._pick_exit_gate(traffic)
            pilot.exit_gate = gate
            turn = "right"
            if self.airfield is not None:
                turn = self.airfield.exit_turn(gate, self.runway)
            key = "departure_exit" if turn else "departure_exit_straight"
            reply = self._say(key, callsign, gate=self._exit_label(gate),
                              turn=turn)
            pilot.last_clearance = reply
            return reply
        # Post-landing: taxi to parking (to a named ramp, or the nearest one).
        # Must come before the taxi readback, which also matches "taxi".
        # Guarded by `parking_cleared`: the readback ("cleared taxi to ramp
        # north") contains "to ramp" and would otherwise re-issue the clearance
        # forever.
        if not pilot.parking_cleared and re.search(
                r"\b(taxi to parking|to parking|to the ramp|to ramp|"
                r"taxi to the ramp)\b", low):
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
                     r"|\btaxi(?:ing)?\b.*\b(startup|start up|start|runway)\b",
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
            if pilot.phase == Phase.DEPARTURE:
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
                pilot.phase = Phase.DEPARTURE
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
                pilot.phase = Phase.DEPARTURE
                pilot.awaiting_takeoff = False
                return self._takeoff_clearance(callsign, pilot)
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        # Departure handoff: once airborne, Tower hands the flight to Control.
        # "rolling" alone is NOT a departure call — a landing pilot says
        # "rolling off runway 25" (handled below), so require a departure word.
        # "wheels up" is the MA departure report; guard it against landing
        # phrases ("wheels up on the go" after a touch-and-go is not one).
        if re.search(r"\b(airborne|departing|leaving|departed|on the way out|"
                     r"taking off|wheels up|off the ground)\b", low) \
                and not re.search(r"\b(vacated|rolling off|rolling out|"
                                  r"touch and go|on the go|go around)\b", low):
            pilot.phase = Phase.AIRBORNE
            return self._say("contact_control", callsign)
        # Readback of "report runway in sight" ("Report runway in sight, Adder11")
        # is NOT the runway-in-sight report itself — only the actual report (said
        # when the runway is genuinely in sight) triggers the break clearance.
        # Must come before the runway-in-sight branch.
        if re.search(r"\b(report|reporting|wilco|roger|copy)\b.*"
                     r"\brunway in sight\b", low):
            return self._say("roger", callsign, agency=self.tower_short)
        if re.search(r"\b(runway in sight|runway insight|visual)\b", low):
            pilot.phase = Phase.LANDING
            pilot.break_cleared = True
            return self._say("cleared_overhead", callsign,
                             flight=self._formation_prefix(pilot))
        # Arrival check-in at the entry point: "Tower, Adder11, Entry East" ->
        # "report runway in sight". STT often drops "entry" (e.g. "Chevy 1 and
        # 3 East"), so once the flight is inbound we also accept a bare gate
        # direction as the entry report.
        if re.search(r"\bentry\b", low) or (
                pilot.phase in (Phase.INBOUND, Phase.LANDING)
                and self._spoken_gate(low)):
            pilot.phase = Phase.INBOUND
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
        if pilot.phase == Phase.DEPARTURE and re.search(
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
            # Cross-check: only clear to land if actually on final.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_on_final(track.lat, track.lon,
                                                      track.heading):
                return self._challenge(callsign, self.tower_short, track)
            # Traffic: if the runway is occupied, sequence instead of clearing.
            if self._runway_busy(traffic, exclude=speaker):
                pilot.phase = Phase.INBOUND
                # Held on approach: the bot re-clears when the runway clears.
                pilot.awaiting_landing = True
                pilot.landing_ready_announced = False
                return self._say("continue_approach", callsign)
            pilot.phase = Phase.LANDING
            pilot.cleared_landing = True
            # Cleared: no longer waiting on the runway to clear.
            pilot.awaiting_landing = False
            pilot.landing_ready_announced = False
            # Master Arms: the landing clearance is valid for the whole flight
            # ("they don't need individual clearances"). Mark same-flight
            # wingmen who are already in the arrival flow as covered, so their
            # own "on final" gets silence instead of a duplicate clearance.
            for wing in self._flight_members(callsign):
                wing_state = self.pilots.get(wing)
                if wing_state is not None and wing_state.phase in (
                        Phase.INBOUND, Phase.LANDING):
                    wing_state.cleared_landing = True
            return self._say("cleared_land", callsign,
                             flight=self._formation_prefix(pilot))
        if re.search(r"\binbound\b|\bon approach\b|\blanding\b|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            return self._inbound_reply(callsign, pilot, track, low)
        return None

    # ---------- Control ----------

    def _handle_control(self, callsign: str, pilot: PilotState, low: str,
                        track: AircraftTrack | None) -> str | None:
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
                             heading=f"{heading:03.0f}", turn=turn)
        # Readback of the join clearance ("150 to join via Entry East") ->
        # Control issues the descent to 1500 ft (once). The pilot's readback of
        # *that* descent then hands the flight to Tower — but only once they are
        # actually at 1500 (the readback is just the acknowledgement; they may
        # still be at Angels 8). An explicit entry report is handled below.
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
                # handoff to Tower waits until the final descent has been given.
                return self._descend_reply(callsign, pilot, track)
            if not self._at_pattern_alt(track):
                pilot.descend_readback = True
                return ""
            pilot.phase = Phase.LANDING
            return self._say("contact_tower_from_control", callsign)
        # Already inbound / handed off, and the pilot reports being at or near
        # the entry point -> hand to Tower. NEVER re-issue the join here: once
        # the join has been given, an "entry" mention must not loop the arrival
        # back to the start (it did — any call containing "entry" re-issued the
        # join regardless of phase, so a handoff could be undone).
        if pilot.phase in (Phase.INBOUND, Phase.LANDING) and re.search(
                r"\b(passing|entering|at the entry|entry|established|abeam)\b",
                low):
            # Cross-check the report against the live position: a pilot who
            # claims to be at the entry point but is still far away is
            # challenged, and is NOT handed to Tower (they would arrive
            # unannounced). The named gate is used if given, else the assigned
            # one. Without a live track we trust the pilot.
            gate = self._spoken_gate(low) or pilot.entry_gate
            if gate and not self._at_entry(track, gate):
                return self._challenge(callsign, self.control_short, track)
            pilot.phase = Phase.LANDING
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
                             heading=f"{heading:03.0f}", turn=turn)
        # Departure check-in ("airborne, 5 miles east climbing" / "at 1500 ft"):
        # radar contact and a climb clearance. Only the *first* check-in gets the
        # climb — a readback or a follow-up level report ("climbing angels 15",
        # "at angels 16") is acknowledged, not re-cleared, so the pilot is not
        # handed an endless "climb to Angels 15".
        if re.search(r"\b(airborne|climbing|departing|departed|level|"
                     r"angels|on the way|at \d+|checking in|with you)\b", low):
            pilot.phase = Phase.AIRBORNE
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
        if re.search(r"\b(on final|final|runway in sight|visual|overhead|"
                     r"break|initial|passing|entering)\b", low):
            pilot.phase = Phase.LANDING
            return self._say("contact_tower_from_control", callsign)
        # Initial Control check-in, bare: "Kutaisi Control, Adder11" (Master Arms
        # kneeboard page 2, step 1) -> "Adder11, Control". Answer it instead of
        # "say again"; the pilot follows with the inbound call.
        # NOTE: exclude "control zone" — a departure readback ("remain below
        # 1500 ft until clear of the control zone") contains "control" and was
        # wrongly treated as an arrival check-in, flipping the phase to Inbound.
        # Airborne pilots (post-departure, before/at the handoff to Control)
        # also say just "Control, callsign" when switching frequency — that is
        # a frequency-change check-in, not an inbound check-in. Don't flip
        # Airborne -> Inbound on it; just answer.
        if re.search(r"\bcontrol\b(?!\s+zone)", low):
            pilot.phase = (Phase.AIRBORNE
                           if pilot.phase == Phase.AIRBORNE else Phase.INBOUND)
            return self._say("control_ack", callsign,
                             agency=self.control_short)
        return None

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
        """True if the aircraft is within `ENTRY_REPORT_RADIUS_NM` of `gate`.

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
        _, distance = bearing_distance(track.lat, track.lon,
                                       target[0], target[1])
        return distance <= ENTRY_REPORT_RADIUS_NM

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
        """The arrival descent, stepped by distance to the entry gate.

        A real controller steps an arrival down rather than dropping it to the
        final altitude 35 miles out. Far from the entry -> an intermediate
        step-down (5000 ft); within `DESCENT_FINAL_NM` -> the final altitude
        (1500 ft). Without a live position we issue the final descent (the
        documented Master Arms example).
        """
        gate = pilot.entry_gate or self._pick_entry_gate(track, None, pilot)
        distance = self._distance_to_gate(track, gate)
        if distance is not None and distance > DESCENT_FINAL_NM:
            if pilot.descend_intermediate:
                # Already stepped down; this is the readback. Stay silent — the
                # next instruction (the final descent) follows once closer, so
                # the controller does not acknowledge (see check_arrival_descent).
                return ""
            pilot.descend_intermediate = True
            return self._say("control_descend_step", callsign,
                             altitude=f"{DESCENT_INTERMEDIATE_FT} feet")
        pilot.descend_issued = True
        return self._say("control_descend", callsign)

    def check_departure_handoff(self, callsign: str,
                                track: AircraftTrack | None) -> str | None:
        """Push a departure to Control once it is clear of the runway zone.

        Master Arms SOP: after takeoff, "expect instructions to switch to
        control" — Tower initiates the handoff, the pilot does not have to
        report "airborne". The handoff waits until the aircraft is past the
        runway (a little time to settle after liftoff), then fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.DEPARTURE or pilot.departure_handoff_announced
                or track is None or self.airfield is None):
            return None
        if self.airfield.is_over_runway(track.lat, track.lon):
            return None
        pilot.departure_handoff_announced = True
        pilot.phase = Phase.AIRBORNE
        # Hand off regardless, but coach if the pilot did not leave via the
        # assigned exit (a trainer note, not a block).
        note = self._exit_deviation_note(pilot, track)
        return note + self._say("contact_control", callsign)

    def _exit_deviation_note(self, pilot: PilotState,
                             track: AircraftTrack | None) -> str:
        """A trainer note if the departure is not near its assigned exit gate.

        Empty when the pilot is close to the assigned gate (or we cannot tell).
        """
        if track is None or self.airfield is None or not pilot.exit_gate:
            return ""
        distance = self._distance_to_gate(track, pilot.exit_gate)
        if distance is None or distance <= EXIT_REPORT_RADIUS_NM:
            return ""
        return self._say("exit_deviation", pilot.callsign,
                         gate=self._exit_label(pilot.exit_gate)) + " "

    def check_departure_climb(self, callsign: str,
                              track: AircraftTrack | None) -> str | None:
        """Climb a departure once it is clear of the control zone.

        A departure that checks in with Control while still inside the CTR is
        told to remain below the ceiling (Tower owns the zone). Real Control
        then watches on radar and issues the climb as soon as the aircraft
        leaves the zone — the pilot does not have to report again. Fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.AIRBORNE or pilot.climb_issued
                or track is None):
            return None
        if self._inside_ctr(track):
            return None
        pilot.climb_issued = True
        return self._say("control_climb", callsign)

    def check_stingray_handoff(self, callsign: str,
                               track: AircraftTrack | None) -> str | None:
        """Push a climbed departure to Stingray once they reach Angels 15.

        Master Arms SOP (kneeboard page 1): after the climb to Angels 15,
        Control hands the flight to Stingray on channel 3. We do not simulate
        AWACS, but the handoff still fires so the radio sequence matches the
        card. Control then stays quiet until the inbound call. Fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.AIRBORNE or not pilot.climb_issued
                or pilot.stingray_handoff_announced or track is None):
            return None
        if self._inside_ctr(track):
            return None
        if track.alt_ft < ANGELS_HANDOFF_FT - ANGELS_HANDOFF_SLOP_FT:
            return None
        pilot.stingray_handoff_announced = True
        return self._say("contact_stingray", callsign)

    def check_arrival_descent(self, callsign: str,
                              track: AircraftTrack | None) -> str | None:
        """Issue the final arrival descent once the aircraft is close enough.

        After the intermediate step-down (issued far out), Control calls the
        final "descend to 1500 feet" when the aircraft reaches the entry area —
        so the pilot is not left at the intermediate altitude. Fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.INBOUND or pilot.descend_issued
                or not pilot.descend_intermediate):
            return None
        gate = pilot.entry_gate or self._pick_entry_gate(track, None, pilot)
        distance = self._distance_to_gate(track, gate)
        if distance is not None and distance <= DESCENT_FINAL_NM:
            pilot.descend_issued = True
            return self._say("control_descend", callsign)
        return None

    def check_descent_handoff(self, callsign: str,
                              track: AircraftTrack | None) -> str | None:
        """Hand an arrival to Tower once it is actually at 1500 ft.

        After "descend to 1500 feet" the pilot reads it back, but they may still
        be at Angels 8. Control waits until the aircraft is at pattern altitude,
        then pushes them to Tower unprompted. Fires once.
        """
        pilot = self._pilot(callsign)
        if (pilot.phase != Phase.INBOUND or not pilot.descend_issued
                or not pilot.descend_readback or track is None):
            return None
        if not self._at_pattern_alt(track):
            return None
        pilot.descend_readback = False
        pilot.phase = Phase.LANDING
        return self._say("contact_tower_from_control", callsign)

    def _spoken_parking(self, low: str) -> str | None:
        """Parking area named in the transmission, e.g. 'ramp north'."""
        if self.airfield is None:
            return None
        for area in self.airfield.parking_areas:
            if re.search(rf"\b{re.escape(area.lower())}\b", low):
                return area
        return None

    def _help(self, callsign: str, pilot: PilotState) -> str:
        """A short, state-aware hint for a pilot who is unsure what to do next.

        Triggered by "<callsign> help". The hint depends on the pilot's current
        phase, so it always tells them the *next* call to make.
        """
        key = {
            Phase.IDLE: "help_idle",
            Phase.CLEARANCE: "help_clearance",
            Phase.TAXI: "help_taxi",
            Phase.HOLDING: "help_holding",
            Phase.LINEUP: "help_lineup",
            Phase.DEPARTURE: "help_departure",
            Phase.AIRBORNE: "help_airborne",
            Phase.INBOUND: "help_inbound",
            Phase.LANDING: "help_landing",
        }.get(pilot.phase, "help_idle")
        hint = self._say(key, callsign, gate=pilot.exit_gate or "Exit East")
        # Always remind the pilot of the escape hatches, so they can never get
        # stuck in a wrong state during training.
        return f"{hint} {self._say('help_escape', callsign)}"

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
        return self._say("vectors", callsign, agency=agency,
                         heading=f"{heading:03.0f}", vector_runway=runway)

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

    def check_final(self, callsign: str, on_final: bool,
                    runway_occupied: bool) -> str | None:
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
            return self._say("go_around", callsign)
        return None

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
        this was added).
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
        if pilot.phase in (Phase.LINEUP, Phase.DEPARTURE, Phase.AIRBORNE,
                           Phase.LANDING) or (
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
        pilot.phase = Phase.DEPARTURE
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
            return self._say("contact_ground", callsign)
        return None
