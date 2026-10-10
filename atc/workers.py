"""Per-controller worker threads.

Each controller (Ground / Tower / Control) gets its own worker thread with its
own queue, so a slow STT or TTS on one frequency never blocks another. All
workers **share** one `AtcBrain` (per-pilot state) and one `CtrTracker`, guarded
by a single lock, so a pilot's state is consistent no matter which controller
they are talking to.

The SRS client's `on_transmission_end` callback only enqueues (fast, never
blocks the receive thread); the worker does the slow work (Whisper STT, brain,
Piper TTS, transmit).
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from brain import AtcBrain, Controller
from ctr import AircraftTrack


# PilotState fields worth watching for a debug state-change trail, and a short
# display label for each.
_STATE_FIELDS = {
    "phase": "phase",
    "entry_gate": "entry",
    "exit_gate": "exit",
    "cleared_inbound": "inbound",
    "cleared_landing": "landing",
    "descend_issued": "descend",
    "climb_issued": "climb",
    "formation": "formation",
    "go_around_issued": "goaround",
    "altitude_warned": "altwarn",
    "incursion_warned": "incursion",
    "awaiting_takeoff": "awaitto",
    "awaiting_landing": "awaitland",
}


def _state_snapshot(brain: AtcBrain) -> dict[str, dict[str, object]]:
    """A comparable snapshot of every pilot's state fields."""
    snap: dict[str, dict[str, object]] = {}
    for callsign, pilot in brain.pilots.items():
        snap[callsign] = {
            field: (pilot.phase.value if field == "phase" else getattr(pilot, field))
            for field in _STATE_FIELDS
        }
    return snap


def _state_changes(before: dict[str, dict[str, object]],
                   after: dict[str, dict[str, object]]) -> list[str]:
    """Human-readable 'callsign: field old->new' lines for a debug trail."""
    out: list[str] = []
    for callsign, now in after.items():
        was = before.get(callsign)
        if was is None:
            out.append(f"{callsign} [new] phase={now['phase']}")
            continue
        diffs = [f"{_STATE_FIELDS[f]} {was[f]}->{now[f]}"
                 for f in now if was.get(f) != now[f]]
        if diffs:
            out.append(f"{callsign}: " + ", ".join(diffs))
    return out


@dataclass
class SharedState:
    """State shared by every controller worker.

    `lock` guards `brain` and any tracker mutation, so two workers can never
    interleave a read-modify-write on the same pilot's state.
    """

    brain: AtcBrain
    lock: threading.RLock
    track_for: Callable[[str], AircraftTrack | None]
    transcribe: Callable[[bytes, str, float, Controller], str]
    speak: Callable[..., None]
    log: Callable[[str], None]
    debug: Callable[[str], None] = lambda _line: None
    traffic: Callable[[], list] = lambda: []


class ControllerWorker:
    """One thread per controller frequency: STT -> brain -> TTS -> transmit."""

    def __init__(self, controller: Controller, freq_hz: int,
                 shared: SharedState, voice=None):
        self.controller = controller
        self.freq_hz = freq_hz
        self.shared = shared
        self.voice = voice  # Piper voice for this controller (immersion)
        self.queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"atc-{controller.value}")

    def start(self) -> None:
        self._thread.start()

    def submit(self, who: str, pcm: bytes, duration: float) -> None:
        """Enqueue a finished transmission (called from the SRS rx thread)."""
        self.queue.put((who, pcm, duration))

    def _run(self) -> None:
        while True:
            who, pcm, duration = self.queue.get()
            try:
                self._handle(who, pcm, duration)
            except Exception as error:  # keep the worker alive
                self.shared.log(f"{self.controller.value} worker error: {error}")
            finally:
                self.queue.task_done()

    def _handle(self, who: str, pcm: bytes, duration: float) -> None:
        tag = f"{self.controller.value}@{self.freq_hz / 1e6:.3f}"
        text = self.shared.transcribe(pcm, who, duration, self.controller)
        if not text:
            return
        track = self.shared.track_for(who)
        traffic = self.shared.traffic()
        with self.shared.lock:
            before = _state_snapshot(self.shared.brain)
            reply = self.shared.brain.handle(
                text, track, controller=self.controller, traffic=traffic,
                speaker=who)
            callsign = self.shared.brain.callsigns.extract(text)
            if callsign:
                self.shared.brain.remember_speaker(who, callsign)
            after = _state_snapshot(self.shared.brain)
            changes = _state_changes(before, after)
        if changes:
            self.shared.debug(f"[{tag}] state: " + " | ".join(changes))
        state = self.shared.brain.pilots.get(callsign or "")
        if reply:
            self.shared.debug(
                f"[{tag}] {who} -> {reply!r}"
                + (f"  [phase={state.phase.value} entry={state.entry_gate!r} "
                   f"exit={state.exit_gate!r}]" if state else ""))
            self.shared.speak(reply, self.freq_hz, self.voice, self.controller)
        elif reply == "":
            # A deliberate silent acknowledgement (e.g. "channel 8, push"):
            # the pilot acknowledged the handoff, so we do not transmit.
            self.shared.log(f"[{tag}] {who}: <handoff ack, silent>")
        else:
            self.shared.log(f"[{tag}] {who}: <no matching intent>")
