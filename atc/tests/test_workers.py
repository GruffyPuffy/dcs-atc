"""Controller workers: routing, concurrency, shared pilot state."""

import threading
import time

from brain import Controller
from workers import ControllerWorker, SharedState


def _shared(brain, spoken, log):
    return SharedState(
        brain=brain, lock=threading.RLock(),
        track_for=lambda who: None,
        transcribe=lambda pcm, who, dur, ctrl: pcm.decode(),
        speak=lambda text, freq, voice=None, controller=None:
            spoken.append((controller, freq, text)),
        log=log.append,
        debug=lambda line: None)


def test_worker_routes_to_controller(brain):
    spoken, log = [], []
    shared = _shared(brain, spoken, log)
    ground = ControllerWorker(Controller.GROUND, 250_000_000, shared)
    ground.start()
    ground.submit("PilotA", b"Ground, Colt 1, requesting taxi", 2.0)
    ground.queue.join()
    assert spoken
    controller, freq, text = spoken[0]
    assert controller == Controller.GROUND
    assert freq == 250_000_000
    assert "taxi to runway" in text


def test_workers_share_pilot_state(brain):
    """Ground and Control workers must see the same pilot state."""
    spoken, log = [], []
    shared = _shared(brain, spoken, log)
    ground = ControllerWorker(Controller.GROUND, 250_000_000, shared)
    control = ControllerWorker(Controller.CONTROL, 257_000_000, shared)
    ground.start()
    control.start()
    ground.submit("PilotA", b"Ground, Colt 1, requesting taxi", 2.0)
    control.submit("PilotA", b"Control, Colt 1, inbound 35 miles north", 2.0)
    ground.queue.join()
    control.queue.join()
    # both calls updated the SAME pilot record
    pilot = brain.pilots["Colt 1"]
    assert pilot.entry_gate  # set by Control
    assert len(spoken) == 2


def test_worker_survives_bad_transcription(brain):
    spoken, log = [], []
    shared = _shared(brain, spoken, log)

    def boom(pcm, who, dur, ctrl):
        raise RuntimeError("stt exploded")

    shared.transcribe = boom
    worker = ControllerWorker(Controller.TOWER, 263_000_000, shared)
    worker.start()
    worker.submit("PilotA", b"anything", 2.0)
    worker.queue.join()
    assert any("error" in line for line in log)
    # worker is still alive and can process the next item
    shared.transcribe = lambda pcm, who, dur, ctrl: pcm.decode()
    worker.submit("PilotA", b"Tower, Colt 1, ready for departure", 2.0)
    worker.queue.join()
    assert spoken
