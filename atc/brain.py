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
    exit_gate: str = ""  # assigned departure exit point
    entry_gate: str = ""  # assigned arrival entry point


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

    def _say(self, key: str, callsign: str, **fields: object) -> str:
        return self.phraseology.render(
            key, callsign=callsign, tower=self.tower_short,
            runway=self.runway, wind=self.wind, ground=self.ground_short,
            control=self.control_short, **fields)

    def _challenge(self, callsign: str, agency: str,
                   track: AircraftTrack) -> str:
        """A realistic 'I don't show you there' challenge for a bad position
        report. Uses the live position so the pilot learns to report correctly."""
        # Very close to the field reads better as "on the airfield" than
        # "0 miles north".
        position = ("on the airfield" if track.distance_nm < 1.0
                    else track.relative)
        return self._say("position_challenge", callsign, agency=agency,
                         position=position)

    def handle(self, text: str, track: AircraftTrack | None = None,
               controller: Controller = Controller.TOWER) -> str | None:
        """Return the ATC reply for a pilot transmission, or None if we
        did not understand it (no callsign, unknown request).

        `track` is the aircraft's live position (if known), used to make
        inbound replies distance-aware. `controller` is which agency the
        transmission was addressed to (from the frequency it arrived on).
        """
        callsign = self.callsigns.extract(text)
        if not callsign:
            return None
        low = text.lower()
        pilot = self._pilot(callsign)

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

        if controller == Controller.GROUND:
            reply = self._handle_ground(callsign, pilot, low, track)
        elif controller == Controller.CONTROL:
            reply = self._handle_control(callsign, pilot, low, track)
        else:
            reply = self._handle_tower(callsign, pilot, low, track)
        if reply is not None:
            return reply

        # shared intents, valid on any frequency
        if re.search(r"\bchecking (in|out)\b|\bwith you\b|\babort(s|ing)?\b", low):
            return self._say("roger", callsign)
        if re.search(r"\b(reading back|roger|wilco|copy)\b", low):
            return self._say("roger", callsign)
        # bare callsign / unintelligible request: ask them to say again
        return self._say("say_again", callsign)

    # ---------- Ground ----------

    def _handle_ground(self, callsign: str, pilot: PilotState,
                       low: str, track: AircraftTrack | None) -> str | None:
        if re.search(r"\b(clearance|ready to copy|ifr)\b", low):
            pilot.phase = Phase.CLEARANCE
            gate = self._pick_exit_gate()
            pilot.exit_gate = gate
            return self._say("departure_exit", callsign, gate=gate, turn="right")
        # Post-landing: taxi to parking (to a named ramp, or the nearest one).
        if re.search(r"\b(taxi to parking|to parking|to the ramp|to ramp|"
                     r"taxi to the ramp)\b", low):
            pilot.phase = Phase.TAXI
            parking = self._spoken_parking(low)
            if parking is None and track is not None and self.airfield is not None:
                parking = self.airfield.nearest_parking_area(track.lat, track.lon)
            route = None
            if track is not None and self.airfield is not None:
                route = self.airfield.nearest_taxi_route(track.lat, track.lon)
            return self._say("taxi_parking", callsign,
                             parking=parking or "the ramp",
                             taxi_route=route or "alpha")
        if re.search(r"\b(request(?:ing)?|asking for|like)\b.*\btaxi\b"
                     r"|\btaxi\b.*\b(startup|start up|start|runway)\b", low):
            pilot.phase = Phase.TAXI
            # Pick the taxi route nearest the aircraft (DCS has no taxiway
            # names, so routes are configured per airfield).
            route = None
            if track is not None and self.airfield is not None:
                route = self.airfield.nearest_taxi_route(track.lat, track.lon)
            return self._say("taxi", callsign, taxi_route=route or "alpha")
        if re.search(r"\bhold(?:ing)? short\b", low) and pilot.phase == Phase.TAXI:
            # Cross-check the report against the live position: a pilot who
            # claims to be holding short but is still on the ramp gets
            # challenged, and their state does NOT advance.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_at_runway(track.lat, track.lon):
                return self._challenge(callsign, self.ground_short, track)
            pilot.phase = Phase.HOLDING
            return self._say("contact_tower", callsign)
        return None

    # ---------- Tower ----------

    def _handle_tower(self, callsign: str, pilot: PilotState, low: str,
                      track: AircraftTrack | None) -> str | None:
        # "ready for departure/takeoff" is unambiguous; a bare "ready" only
        # counts once Ground has advanced the phase (avoids false positives).
        if re.search(r"\bready for departure\b|\bready for takeoff\b", low):
            # Cross-check: a pilot who says "ready for departure" but is not at
            # the runway gets challenged, and is NOT cleared for takeoff.
            if track is not None and self.airfield is not None \
                    and not self.airfield.is_at_runway(track.lat, track.lon):
                return self._challenge(callsign, self.tower_short, track)
            pilot.phase = Phase.DEPARTURE
            return self._say("takeoff", callsign)
        if re.search(r"\bready\b", low) and pilot.phase in (
                Phase.TAXI, Phase.HOLDING, Phase.CLEARANCE):
            pilot.phase = Phase.DEPARTURE
            return self._say("takeoff", callsign)
        # Departure handoff: once airborne, Tower hands the flight to Control.
        if re.search(r"\b(airborne|departing|leaving|departed|on the way out)\b",
                     low):
            pilot.phase = Phase.AIRBORNE
            return self._say("contact_control", callsign)
        if re.search(r"\b(runway in sight|runway insight|visual)\b", low):
            return self._say("cleared_overhead", callsign)
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
            pilot.phase = Phase.LANDING
            pilot.cleared_landing = True
            return self._say("cleared_land", callsign)
        if re.search(r"\binbound\b|\bon approach\b|\blanding\b|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            return self._inbound_reply(callsign, pilot, track, low)
        return None

    # ---------- Control ----------

    def _handle_control(self, callsign: str, pilot: PilotState, low: str,
                        track: AircraftTrack | None) -> str | None:
        # Departure check-in ("airborne, 5 miles east climbing"): just radar
        # contact. Arrival check-in ("inbound 35 miles north"): radar contact +
        # routing to join via an entry point.
        if re.search(r"\b(airborne|climbing|departing|departed)\b", low):
            pilot.phase = Phase.AIRBORNE
            return self._say("control_contact", callsign)
        if re.search(r"\binbound\b|\bchecking in\b|\bwith you\b|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            gate = self._pick_entry_gate(track, low)
            pilot.entry_gate = gate
            return self._say("control_join", callsign, gate=gate)
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
        """Departure exit point (first configured gate, or a default)."""
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
            Phase.DEPARTURE: "help_departure",
            Phase.AIRBORNE: "help_airborne",
            Phase.INBOUND: "help_inbound",
            Phase.LANDING: "help_landing",
        }.get(pilot.phase, "help_idle")
        return self._say(key, callsign, gate=pilot.exit_gate or "Exit East")

    def _directions(self, callsign: str, low: str,
                    track: AircraftTrack | None,
                    controller: Controller) -> str:
        """Bearing/distance to a named gate (or the field) from the pilot's
        live position. A trainer aid to help find the CTR entry/exit points."""
        agency = {Controller.GROUND: self.ground_short,
                  Controller.CONTROL: self.control_short,
                  Controller.TOWER: self.tower_short}.get(controller,
                                                          self.tower_short)
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
        agency = {Controller.GROUND: self.ground_short,
                  Controller.CONTROL: self.control_short,
                  Controller.TOWER: self.tower_short}.get(controller,
                                                          self.tower_short)
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
