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
                 gate_locator=None):
        self.tower = tower
        self.runway = runway
        self.ground = ground
        self.control = control
        self.gates = gates or []
        self.gate_locator = gate_locator  # callable(lat, lon) -> gate name
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
            key, callsign=callsign, tower=self.tower, runway=self.runway,
            wind=self.wind, ground=self.ground, control=self.control, **fields)

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

        if controller == Controller.GROUND:
            reply = self._handle_ground(callsign, pilot, low)
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
                       low: str) -> str | None:
        if re.search(r"\b(clearance|ready to copy|ifr)\b", low):
            pilot.phase = Phase.CLEARANCE
            gate = self._pick_exit_gate()
            pilot.exit_gate = gate
            return self._say("departure_exit", callsign, gate=gate, turn="right")
        if re.search(r"\b(request(?:ing)?|asking for|like)\b.*\btaxi\b"
                     r"|\btaxi\b.*\b(startup|start up|start|runway)\b", low):
            pilot.phase = Phase.TAXI
            return self._say("taxi", callsign)
        if re.search(r"\bhold(?:ing)? short\b", low) and pilot.phase == Phase.TAXI:
            pilot.phase = Phase.HOLDING
            return self._say("contact_tower", callsign)
        return None

    # ---------- Tower ----------

    def _handle_tower(self, callsign: str, pilot: PilotState, low: str,
                      track: AircraftTrack | None) -> str | None:
        # "ready for departure/takeoff" is unambiguous; a bare "ready" only
        # counts once Ground has advanced the phase (avoids false positives).
        if re.search(r"\bready for departure\b|\bready for takeoff\b", low):
            pilot.phase = Phase.DEPARTURE
            return self._say("takeoff", callsign)
        if re.search(r"\bready\b", low) and pilot.phase in (
                Phase.TAXI, Phase.HOLDING, Phase.CLEARANCE):
            pilot.phase = Phase.DEPARTURE
            return self._say("takeoff", callsign)
        if re.search(r"\b(runway in sight|runway insight|visual)\b", low):
            return self._say("cleared_overhead", callsign)
        if re.search(r"\binbound\b|\bfinal\b|\bon approach\b|\blanding\b"
                     r"|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            return self._inbound_reply(callsign, pilot, track)
        return None

    # ---------- Control ----------

    def _handle_control(self, callsign: str, pilot: PilotState, low: str,
                        track: AircraftTrack | None) -> str | None:
        if re.search(r"\binbound\b|\bchecking in\b|\bwith you\b|\bentry\b", low):
            pilot.phase = Phase.INBOUND
            pilot.cleared_inbound = True
            gate = self._pick_entry_gate(track)
            pilot.entry_gate = gate
            return self._say("control_join", callsign, gate=gate)
        return None

    def _inbound_reply(self, callsign: str, pilot: PilotState,
                       track: AircraftTrack | None) -> str:
        """Distance-aware inbound clearance (outside vs inside the CTR)."""
        if track is None:
            return self._say("report_runway_in_sight", callsign)
        if track.inside:
            return self._say("inbound_inside", callsign, position=track.relative)
        gate = pilot.entry_gate or self._pick_entry_gate(track)
        return self._say("inbound_via_gate", callsign, gate=gate)

    def _pick_exit_gate(self) -> str:
        """Departure exit point (first configured gate, or a default)."""
        gate = self.gates[0] if self.gates else "North"
        return f"Exit {gate}"

    def _pick_entry_gate(self, track: AircraftTrack | None) -> str:
        """Arrival entry point: nearest gate to the aircraft, else a default."""
        if track is not None and self.gate_locator is not None:
            gate = self.gate_locator(track.lat, track.lon)
            if gate:
                return f"Entry {gate}"
        return "Entry East"

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
