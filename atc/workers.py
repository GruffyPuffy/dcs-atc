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
        with self.shared.lock:
            reply = self.shared.brain.handle(
                text, track, controller=self.controller)
            callsign = self.shared.brain.callsigns.extract(text)
            if callsign:
                self.shared.brain.remember_speaker(who, callsign)
            state = self.shared.brain.pilots.get(callsign or "")
        if reply:
            self.shared.debug(
                f"[{tag}] {who} -> {reply!r}"
                + (f"  [phase={state.phase.value} entry={state.entry_gate!r} "
                   f"exit={state.exit_gate!r}]" if state else ""))
            self.shared.speak(reply, self.freq_hz, self.voice, self.controller)
        else:
            self.shared.log(f"[{tag}] {who}: <no matching intent>")
