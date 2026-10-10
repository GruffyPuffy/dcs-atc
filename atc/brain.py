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
from callsigns import CallsignRegistry
from ctr import AircraftTrack, CtrEvent
from airspace import bearing_distance

TOWER = "Kutaisi Tower"
RUNWAY = "25"
DEFAULT_PHRASEOLOGY = Path(__file__).with_name("phraseology.json")


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
    go_around_issued: bool = False
    descend_issued: bool = False  # Control has issued the descent to 1500 ft
    climb_issued: bool = False  # Control has issued the departure climb
    altitude_warned: bool = False  # warned about busting the CTR ceiling
    incursion_warned: bool = False  # warned about being on the runway uncleared
    exit_gate: str = ""  # assigned departure exit point
    entry_gate: str = ""  # assigned arrival entry point
    # Held because the runway was occupied; the bot calls the pilot back when it
    # clears. `*_ready_announced` fires the unprompted "runway is clear" once.
    awaiting_takeoff: bool = False
    awaiting_landing: bool = False
    takeoff_ready_announced: bool = False
    landing_ready_announced: bool = False
    formation: int = 0  # flight size (2+ = multi-ship), learned from calls
    last_reply: str = ""  # last clearance, replayed on "say again"
    last_clearance: str = ""  # last clearance issued, for readback verification
    last_controller: str = ""  # last agency talked to (for the map view)
    readback_fails: int = 0  # consecutive incomplete readbacks of one clearance


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
        self.wind = "calm"  # updated from live weather
        self.qnh = "2992"  # updated from live weather (inHg, 4 digits)
        # SRS speaker name -> flight callsign, learned from transmissions, so
        # automatic calls (CTR warning, go-around) can address the pilot by
        # callsign instead of the raw DCS unit name.
        self.speaker_callsigns: dict[str, str] = {}
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
        """
        if not who or not callsign:
            return
        for key in self.callsigns.speaker_spellings(who):
            self.speaker_callsigns[key] = callsign

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

    def extract_callsign(self, text: str, speaker: str = "") -> str | None:
        """The callsign a transmission is *from*, or None.

        Prefers the speaker's own known callsign when the text names it, so a
        flight name that merely appears in the message (a readback "descend to
        Enfield 15" = heading 150 to Enfield) does not hijack the transmission;
        otherwise the first callsign named. A speaker never established still
        uses the first match, and a call with no callsign returns None (the
        caller may then fall back to `_resolve_speaker`).
        """
        found = self.callsigns.extract_all(text)
        if not found:
            return None
        own = self.callsign_for_speaker(speaker) if speaker else ""
        if own and own != speaker and own in found:
            return own
        return found[0]

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
        """Update the spoken wind (e.g. '270 at 5')."""
        if speed < 0.5:
            self.wind = "calm"
        else:
            self.wind = f"{direction:03.0f} at {speed:.0f}"

    def set_qnh(self, qnh_inhg: float) -> None:
        """Update the spoken QNH (Master Arms read it as 4 digits, e.g. 2992)."""
        if qnh_inhg:
            self.qnh = f"{round(qnh_inhg * 100):04d}"

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
        return self.phraseology.render(
            key, callsign=callsign, tower=self.tower_short,
            agency=agency or self.tower_short,
            runway=self.runway, wind=self.wind, ground=self.ground_short,
            control=self.control_short, qnh=self.qnh, **overrides, **fields)

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
    #
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

    def _readback_item(self, low: str, clearance: str) -> str | None:
        """The label of the first item the pilot failed to read back, or None.

        Only the clearance's numbers (runway/QNH/altitude/heading) and gate names
        are required — the filler wording is ignored, so a normal abbreviated
        readback passes.
        """
        clearance = clearance or ""
        spoken = self._numbers(low)
        missing_num = [n for n in self._numbers(clearance) if n not in spoken]
        gates = [g for g in self.gates if g.lower() in clearance.lower()]
        missing_gate = [g for g in gates if g.lower() not in low]
        if not missing_num and not missing_gate:
            return None
        return self._item_label(clearance, missing_num, missing_gate)

    def _check_readback(self, callsign, pilot, low, clearance, controller,
                        ok_key="readback_correct"):
        """Confirm a readback, or flag it incomplete — but never block.

        A complete readback is confirmed ("readback correct"). An incomplete one
        is **not** called correct: it gets an honest "readback incomplete —
        {item} not read back, continue." so the pilot knows what to train on, and
        the caller still advances the phase, so a fumbled readback never traps
        them — they keep flying and fix it next time.
        """
        agency = self._agency(controller)
        item = self._readback_item(low, clearance)
        if item is None:
            pilot.readback_fails = 0
            return self._say(ok_key, callsign, agency=agency)
        pilot.readback_fails += 1
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
        # Every callsign the text names, in order. A transmission can mention
        # several (a readback "descend to Enfield 15" names Enfield; a formation
        # call names two flights). Prefer the *speaker's own* callsign when it is
        # among them, so a content flight name does not hijack the transmission
        # (this was "Climbing to Enfield 15" being handled as flight Enfield).
        callsign = self.extract_callsign(text, speaker)
        if not callsign:
            # Some calls may omit the flight number (e.g. "Colt help").
            name = self.callsigns.extract_name(text)
            if name and re.search(r"\b(help|assist|what do i do|what now|"
                                  r"remind me)\b", text.lower()):
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
            if self.solo_mode:
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
        # Trainer aid: "<callsign> help" returns a short, state-aware hint.
        if re.search(r"\b(help|assist|what do i do|what now|remind me)\b", low):
            return self._help(callsign, pilot)

        # Trainer aid: "<callsign> request vectors [for runway 25]" gives an
        # approach vector ("fly heading X, vectors for runway Y"). Real ATC
        # phraseology — distinct from a bearing/distance request.
        if re.search(r"\bvectors?\b", low):
            return self._vectors(callsign, low, track, controller)

        # Trainer aid: "<callsign> request bearing and distance [to <gate>]"
        # gives a bearing/distance to a named entry/exit gate (or the field).
        if re.search(r"\b(bearing|directions?|how do i get|where is)\b", low):
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
            reply = self._handle_ground(callsign, pilot, low, track)
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
            r"lined up|waiting|turn|heading|descend|climb|angels|exit|via|"
            r"in use|qnh|q\.?n\.?h\.?)\b",
            low)
        if readback and pilot.phase == Phase.LINEUP:
            # Readback of "line up and wait" -> the takeoff clearance. The
            # takeoff is issued regardless; coaching is appended if the line-up
            # readback is incomplete, so the pilot never has to repeat it.
            coach = self._readback_coaching(callsign, pilot, low,
                                            pilot.last_clearance or pilot.last_reply,
                                            controller)
            pilot.phase = Phase.DEPARTURE
            return coach + self._say("lineup_readback", callsign, turnout="right")
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

    def _reset(self, callsign: str, pilot: PilotState,
               controller: Controller) -> str:
        """Full reset to the default state (trainer escape hatch)."""
        pilot.phase = Phase.IDLE
        pilot.cleared_inbound = False
        pilot.cleared_landing = False
        pilot.go_around_issued = False
        pilot.descend_issued = False
        pilot.climb_issued = False
        pilot.exit_gate = ""
        pilot.entry_gate = ""
        pilot.readback_fails = 0
        pilot.awaiting_takeoff = False
        pilot.awaiting_landing = False
        pilot.takeoff_ready_announced = False
        pilot.landing_ready_announced = False
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
        pilot.descend_issued = False
        pilot.awaiting_takeoff = False
        pilot.awaiting_landing = False
        return self._say("cancel_ack", callsign,
                         agency=self._agency(controller))

    # ---------- Ground ----------

    def _handle_ground(self, callsign: str, pilot: PilotState,
                       low: str, track: AircraftTrack | None) -> str | None:
        # Readback of the runway-in-use/QNH info ("25 in use, QNH 2992") ->
        # "readback correct, advise when ready for clearance". Without this the
        # line has no trigger (none of {information, taxi, clearance}) and the
        # pilot gets "say again", stalling the flow at the very first exchange.
        if pilot.phase == Phase.IDLE and re.search(
                r"\b(in use|qnh|q\.?n\.?h\.?)\b", low):
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
            gate = self._pick_exit_gate()
            pilot.exit_gate = gate
            turn = "right"
            if self.airfield is not None:
                turn = self.airfield.exit_turn(gate, self.runway)
            key = "departure_exit" if turn else "departure_exit_straight"
            reply = self._say(key, callsign, gate=gate, turn=turn)
            pilot.last_clearance = reply
            return reply
        # Post-landing: taxi to parking (to a named ramp, or the nearest one).
        # Must come before the taxi readback, which also matches "taxi".
        if re.search(r"\b(taxi to parking|to parking|to the ramp|to ramp|"
                     r"taxi to the ramp)\b", low):
            pilot.phase = Phase.TAXI
            parking = self._spoken_parking(low)
            if parking is None and track is not None and self.airfield is not None:
                parking = self.airfield.nearest_parking_area(track.lat, track.lon)
            route = None
            if self.airfield is not None:
                lat = track.lat if track is not None else None
                lon = track.lon if track is not None else None
                route = self.airfield.parking_route(lat, lon)
            return self._say("taxi_parking", callsign,
                             parking=parking or "the ramp",
                             taxi_route=route or "Whiskey")
        # Readback of the taxi clearance ("cleared taxi Sierra Echo and hold
        # short runway 25") -> "readback correct". Must come before the taxi
        # request and the hold-short report (which also say "hold short").
        if pilot.phase == Phase.TAXI and re.search(
                r"\b(clear|cleared|copy|roger|via|taxi)\b", low) \
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
        if re.search(r"\bhold(?:ing)? short\b", low) and pilot.phase == Phase.TAXI:
            # Cross-check the report against the live position: a pilot who
            # claims to be holding short but is still on the ramp gets
            # challenged, and their state does NOT advance.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_holding_short(track.lat, track.lon):
                return self._challenge(callsign, self.ground_short, track)
            pilot.phase = Phase.HOLDING
            # Name the holding position the pilot is actually at (P1..P4).
            holding = self._holding_point(track)
            if holding:
                return self._say("contact_tower_holding", callsign,
                                 holding=holding)
            return self._say("contact_tower", callsign)
        # Initial check-in: "Ground, Adder11" -> "Adder11, Ground". If the pilot
        # gives a position/formation ("two-ship Hornets on Ramp South") without
        # "with information X", Ground also passes the runway/QNH.
        if re.search(r"\b(ramp|apron|parking|taxiway|ship|hornets?|vipers?|"
                     r"information)\b", low):
            if re.search(r"\binformation\b", low):
                return self._say("ground_ack", callsign)
            return self._say("ground_info", callsign)
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
                return self._say("lineup_readback", callsign, turnout="right")
            # Already lined up and waiting: this "ready" call is the natural
            # trigger for the takeoff clearance (the pilot has done the line-up
            # and is telling us they are ready to go). Without this, a pilot who
            # reports ready again instead of reading back "line up and wait"
            # would be stuck in Lineup forever.
            if pilot.phase == Phase.LINEUP:
                # Already lined up: this "ready" call is the natural trigger for
                # the takeoff clearance. Still sequence against traffic.
                if self._runway_busy(traffic, exclude=speaker):
                    pilot.awaiting_takeoff = True
                    pilot.takeoff_ready_announced = False
                    return self._say("hold_short_traffic", callsign)
                pilot.phase = Phase.DEPARTURE
                pilot.awaiting_takeoff = False
                return self._say("lineup_readback", callsign, turnout="right")
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
                pilot.phase = Phase.DEPARTURE
                pilot.awaiting_takeoff = False
                return self._say("lineup_readback", callsign, turnout="right")
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        # Departure handoff: once airborne, Tower hands the flight to Control.
        if re.search(r"\b(airborne|departing|leaving|departed|on the way out|"
                     r"taking off|rolling|rolling out)\b", low):
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
        # the break clearance; "in the break" is a position report.
        if re.search(r"\b(overhead break|initial)\b", low):
            pilot.phase = Phase.LANDING
            return self._say("cleared_overhead", callsign,
                             flight=self._formation_prefix(pilot))
        if re.search(r"\b(in the break|the break|overhead)\b", low):
            pilot.phase = Phase.LANDING
            return self._say("break_ack", callsign)
        # Readback of the takeoff clearance once already cleared ("right turn
        # out, cleared for takeoff, 33, Colt 1"). Must come before the "final"
        # check, so a readback containing "turn" is not misread as a landing.
        if pilot.phase == Phase.DEPARTURE and re.search(
                r"\b(cleared|takeoff|take off|right turn|left turn|turnout)\b",
                low):
            return self._say("roger", callsign, agency=self.tower_short)
        # After landing, Tower hands the flight to Ground.
        if re.search(r"\b(vacated|clear of the runway|off the runway|"
                     r"runway vacated|clear of runway)\b", low):
            pilot.phase = Phase.TAXI
            return self._say("contact_ground", callsign)
        # Readback of the landing clearance ("runway 33, cleared to land") once
        # already cleared — acknowledge, don't "say again" (the pilot is busy
        # landing). Must come before the "on final" branch below.
        if pilot.phase == Phase.LANDING and re.search(
                r"\b(cleared|clear to land|copy|roger|wilco|readback)\b", low):
            return self._say("roger", callsign, agency=self.tower_short)
        # "on final" is a landing clearance (distinct from a general inbound call).
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
            return self._say("control_join_requested", callsign, gate=requested,
                             heading=f"{heading:03.0f}", turn="right")
        # Readback of the join clearance ("150 to join via Entry East") ->
        # Control issues the descent to 1500 ft (once). The pilot's readback of
        # *that* descent then hands the flight to Tower — matching the Master
        # Arms arrival flow, where Control hands off right after the descend
        # readback (there is no separate "passing the entry point" call). An
        # explicit entry report is handled by the branch below.
        if pilot.phase == Phase.INBOUND and re.search(
                r"\b(join|via|heading|turn|descend|1500?)\b", low):
            if not pilot.descend_issued:
                pilot.descend_issued = True
                return self._say("control_descend", callsign)
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
            return self._say("control_join", callsign, gate=gate,
                             heading=f"{heading:03.0f}")
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
                return self._say("readback_correct", callsign,
                                 agency=self.control_short)
            pilot.climb_issued = True
            return self._say("control_climb", callsign)
        # Near the field, Control hands the flight to Tower (e.g. "on final",
        # "runway in sight", "overhead break", "passing the entry point").
        if re.search(r"\b(on final|final|runway in sight|visual|overhead|"
                     r"break|initial|passing|entering)\b", low):
            pilot.phase = Phase.LANDING
            return self._say("contact_tower_from_control", callsign)
        # Initial Control check-in, bare: "Kutaisi Control, Adder11" (Master Arms
        # kneeboard page 2, step 1) -> "Adder11, Control". Answer it instead of
        # "say again"; the pilot follows with the inbound call.
        if re.search(r"\bcontrol\b", low):
            pilot.phase = Phase.INBOUND
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

    def _pick_exit_gate(self) -> str:
        """Departure exit point.

        The gate that best matches the departure direction for the active runway
        (you fly the runway heading after takeoff), falling back to the first
        configured gate. A pilot-named exit is a future addition.
        """
        if self.airfield is not None:
            gate = self.airfield.default_exit_gate(self.runway)
            if gate:
                return f"Exit {gate}"
        gate = self.gates[0] if self.gates else "North"
        return f"Exit {gate}"

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

    def on_ctr_event(self, callsign: str, event: CtrEvent,
                     track: AircraftTrack) -> str | None:
        """React to a CTR boundary crossing. Returns a warning to broadcast,
        or None if no action is needed."""
        pilot = self._pilot(callsign)
        if event == CtrEvent.ENTERED and not pilot.cleared_inbound:
            return self._say("ctr_warning", callsign)
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

        Fires once per incursion (re-arms when off the runway). A pilot in the
        Departure or Landing phase is legitimately on the runway. So is a pilot
        in **Lineup** — "line up and wait" *explicitly* puts them on the runway,
        so warning them to "vacate immediately" would be wrong (and did, before
        this was added).
        """
        pilot = self._pilot(callsign)
        if not on_runway:
            pilot.incursion_warned = False
            return None
        # A pilot in Departure or Landing is legitimately on the runway. So is
        # one in Lineup ("line up and wait" puts them there) and one in
        # **Airborne** — a departing aircraft climbing out over the runway
        # corridor is not an incursion (this fired a false "vacate immediately"
        # right after takeoff, once the pilot called "airborne").
        if pilot.phase in (Phase.LINEUP, Phase.DEPARTURE, Phase.AIRBORNE,
                           Phase.LANDING):
            return None
        if pilot.incursion_warned:
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
