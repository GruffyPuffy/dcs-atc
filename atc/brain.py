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
    altitude_warned: bool = False  # warned about busting the CTR ceiling
    incursion_warned: bool = False  # warned about being on the runway uncleared
    exit_gate: str = ""  # assigned departure exit point
    entry_gate: str = ""  # assigned arrival entry point
    formation: int = 0  # flight size (2+ = multi-ship), learned from calls
    last_reply: str = ""  # last clearance, replayed on "say again"
    last_controller: str = ""  # last agency talked to (for the map view)


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

    def remember_speaker(self, who: str, callsign: str) -> None:
        """Map an SRS speaker name to the callsign they used."""
        if who and callsign:
            self.speaker_callsigns[who.lower()] = callsign

    def callsign_for_speaker(self, who: str) -> str:
        """Flight callsign for an SRS/DCS player name, or the name itself."""
        return self.speaker_callsigns.get(who.lower(), who)

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
        callsign = self.callsigns.extract(text)
        if not callsign:
            # Some calls may omit the flight number (e.g. "Colt help").
            name = self.callsigns.extract_name(text)
            if name and re.search(r"\b(help|assist|what do i do|what now|"
                                  r"remind me)\b", text.lower()):
                return self._help(name, self._pilot(name))
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
            return self._directions(callsign, low, track, controller)

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
            r"in use|qnh)\b",
            low)
        if readback and pilot.phase == Phase.LINEUP:
            # Readback of "line up and wait" -> the takeoff clearance.
            pilot.phase = Phase.DEPARTURE
            return self._say("lineup_readback", callsign, turnout="right")
        if readback and pilot.phase in (Phase.CLEARANCE, Phase.TAXI,
                                        Phase.INBOUND):
            return self._say("readback_correct", callsign, agency=agency)

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

    def _reset(self, callsign: str, pilot: PilotState,
               controller: Controller) -> str:
        """Full reset to the default state (trainer escape hatch)."""
        pilot.phase = Phase.IDLE
        pilot.cleared_inbound = False
        pilot.cleared_landing = False
        pilot.go_around_issued = False
        pilot.descend_issued = False
        pilot.exit_gate = ""
        pilot.entry_gate = ""
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
        return self._say("cancel_ack", callsign,
                         agency=self._agency(controller))

    # ---------- Ground ----------

    def _handle_ground(self, callsign: str, pilot: PilotState,
                       low: str, track: AircraftTrack | None) -> str | None:
        # Readback of the departure clearance ("after departure turn right Exit
        # North, 1500 ft or below") -> "readback correct".
        if pilot.phase == Phase.CLEARANCE and re.search(
                r"\b(after departure|turn|exit|1500|or below|below)\b", low):
            return self._say("readback_correct", callsign,
                             agency=self.ground_short)
        if re.search(r"\b(clearance|ready to copy|ifr)\b", low):
            pilot.phase = Phase.CLEARANCE
            gate = self._pick_exit_gate()
            pilot.exit_gate = gate
            turn = "right"
            if self.airfield is not None:
                turn = self.airfield.exit_turn(gate, self.runway)
            key = "departure_exit" if turn else "departure_exit_straight"
            return self._say(key, callsign, gate=gate, turn=turn)
        # Readback of the taxi clearance ("cleared taxi Sierra Echo and hold
        # short runway 25") -> "readback correct". Must come before the taxi
        # request and the hold-short report (which also say "hold short").
        if pilot.phase == Phase.TAXI and re.search(r"\b(cleared|via)\b", low) \
                and not re.search(r"\bholding\b", low):
            return self._say("readback_correct", callsign,
                             agency=self.ground_short)
        # Post-landing: taxi to parking (to a named ramp, or the nearest one).
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
            return self._say("taxi", callsign, taxi_route=route or "Sierra Echo")
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
        if re.search(r"\bready for departure\b|\bready for takeoff\b", low):
            # Already cleared for takeoff: re-issue the clearance (idempotent)
            # rather than dropping the pilot back to Lineup.
            if pilot.phase == Phase.DEPARTURE:
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
                    return self._say("hold_short_traffic", callsign)
                pilot.phase = Phase.DEPARTURE
                return self._say("lineup_readback", callsign, turnout="right")
            # Cross-check: a pilot who says "ready for departure" but is not at
            # the runway gets challenged, and is NOT cleared to line up.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_holding_short(track.lat, track.lon):
                return self._challenge(callsign, self.tower_short, track)
            # Traffic: do not let them line up if the runway is occupied.
            if self._runway_busy(traffic, exclude=speaker):
                pilot.phase = Phase.HOLDING
                return self._say("hold_short_traffic", callsign)
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        if re.search(r"\bready\b", low) and pilot.phase in (
                Phase.TAXI, Phase.HOLDING, Phase.CLEARANCE, Phase.LINEUP):
            if pilot.phase == Phase.LINEUP:
                # already lined up: a bare "ready" also clears takeoff
                if self._runway_busy(traffic, exclude=speaker):
                    return self._say("hold_short_traffic", callsign)
                pilot.phase = Phase.DEPARTURE
                return self._say("lineup_readback", callsign, turnout="right")
            pilot.phase = Phase.LINEUP
            return self._say("line_up", callsign)
        # Departure handoff: once airborne, Tower hands the flight to Control.
        if re.search(r"\b(airborne|departing|leaving|departed|on the way out|"
                     r"taking off|rolling|rolling out)\b", low):
            pilot.phase = Phase.AIRBORNE
            return self._say("contact_control", callsign)
        if re.search(r"\b(runway in sight|runway insight|visual)\b", low):
            return self._say("cleared_overhead", callsign,
                             flight=self._formation_prefix(pilot))
        # Arrival check-in at the entry point: "Tower, Adder11, Entry East" ->
        # "report runway in sight".
        if re.search(r"\bentry\b", low):
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
        # After landing, Tower hands the flight to Ground.
        if re.search(r"\b(vacated|clear of the runway|off the runway|"
                     r"runway vacated|clear of runway)\b", low):
            pilot.phase = Phase.TAXI
            return self._say("contact_ground", callsign)
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
                return self._say("continue_approach", callsign)
            pilot.phase = Phase.LANDING
            pilot.cleared_landing = True
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
        # Readback of the join clearance ("150 to join via Entry East") ->
        # Control issues the descent to 1500 ft (once). Must come before the
        # inbound regex, which also matches "entry".
        if pilot.phase == Phase.INBOUND and re.search(
                r"\b(join|via|heading|turn|descend)\b", low):
            if not pilot.descend_issued:
                pilot.descend_issued = True
                return self._say("control_descend", callsign)
            return self._say("readback_correct", callsign,
                             agency=self.control_short)
        # Already inbound and reporting they have reached the entry point ->
        # hand to Tower (must come before the generic inbound regex, which also
        # matches "entry"/"inbound").
        if pilot.phase == Phase.INBOUND and re.search(
                r"\b(passing|entering|at the entry|established|abeam)\b", low):
            pilot.phase = Phase.LANDING
            return self._say("contact_tower_from_control", callsign)
        # Arrival check-in ("inbound ...") takes priority over the departure
        # keywords: an inbound call often names an altitude ("inbound 35 miles
        # north at Angels 12"), which must NOT be read as a departure check-in
        # (that would answer a joining aircraft with "climb to Angels 15").
        if re.search(r"\binbound\b|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            gate = self._pick_entry_gate(track, low)
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
        # radar contact and a climb clearance.
        if re.search(r"\b(airborne|climbing|departing|departed|level|"
                     r"angels|on the way|at \d+|checking in|with you)\b", low):
            pilot.phase = Phase.AIRBORNE
            return self._say("control_climb", callsign)
        # Near the field, Control hands the flight to Tower (e.g. "on final",
        # "runway in sight", "overhead break", "passing the entry point").
        if re.search(r"\b(on final|final|runway in sight|visual|overhead|"
                     r"break|initial|passing|entering)\b", low):
            pilot.phase = Phase.LANDING
            return self._say("contact_tower_from_control", callsign)
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

    def _pick_entry_gate(self, track: AircraftTrack | None,
                         low: str = "") -> str:
        """Arrival entry point.

        Priority: the direction the pilot *said* (e.g. "35 miles north" ->
        Entry North), then the gate nearest the aircraft's live position, then
        a default. The spoken direction wins because it is what the pilot
        expects to hear back.
        """
        spoken = self._spoken_gate(low)
        if spoken:
            return spoken
        if track is not None and self.gate_locator is not None:
            gate = self.gate_locator(track.lat, track.lon)
            if gate:
                return f"Entry {gate}"
        return "Entry East"

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
                    controller: Controller) -> str:
        """Bearing/distance to a named gate (or the field) from the pilot's
        live position. A trainer aid to help find the CTR entry/exit points."""
        agency = self._agency(controller)
        if track is None or self.airfield is None:
            return self._say("directions_unknown", callsign, agency=agency)
        # Which gate? Prefer one named in the call, else the nearest.
        gate = self._spoken_gate(low)
        if gate is None and self.gate_locator is not None:
            nearest = self.gate_locator(track.lat, track.lon)
            gate = f"Entry {nearest}" if nearest else None
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
        Departure or Landing phase is legitimately on the runway.
        """
        pilot = self._pilot(callsign)
        if not on_runway:
            pilot.incursion_warned = False
            return None
        if pilot.phase in (Phase.DEPARTURE, Phase.LANDING):
            return None
        if pilot.incursion_warned:
            return None
        pilot.incursion_warned = True
        return self._say("runway_incursion", callsign)
