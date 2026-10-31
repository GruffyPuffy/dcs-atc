"""Controller workers: routing, concurrency, shared pilot state."""

import threading
import time

from brain import Controller, Phase
from workers import ControllerWorker, SharedState, _state_changes, _state_snapshot


def _shared(brain, spoken, log):
    return SharedState(
        brain=brain, lock=threading.RLock(),
        track_for=lambda who: None,
        transcribe=lambda pcm, who, dur, ctrl: pcm.decode(),
        speak=lambda text, freq, voice=None, controller=None, **_kw:
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
    assert "cleared taxi" in text


def test_worker_passes_reply_wait_policy_to_radio_output(brain):
    spoken, log, policies = [], [], []
    shared = _shared(brain, spoken, log)
    shared.speak = lambda *args, **kwargs: policies.append(
        (kwargs["radio_policy"], kwargs["radio_wait"]))
    ground = ControllerWorker(Controller.GROUND, 250_000_000, shared)
    ground.start()

    ground.submit("PilotA", b"Ground, Colt 1, requesting taxi", 2.0)
    ground.queue.join()
    assert policies[-1] == ("answer", 5.0)

    ground.submit("PilotA", b"Ground, Colt 1, declaring emergency", 2.0)
    ground.queue.join()
    assert policies[-1] == ("emergency", 2.0)


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


def test_state_changes_reports_phase_transition(brain):
    """The debug trail names the pilot and the field that changed."""
    before = _state_snapshot(brain)
    brain.handle("Ground, Colt 1, requesting taxi",
                 controller=Controller.GROUND, speaker="PilotA")
    after = _state_snapshot(brain)
    changes = _state_changes(before, after)
    assert any("Colt 1" in line and "phase" in line for line in changes)


def test_state_changes_empty_when_nothing_moves(brain):
    brain.handle("Ground, Colt 1, requesting taxi",
                 controller=Controller.GROUND, speaker="PilotA")
    snap = _state_snapshot(brain)
    assert _state_changes(snap, _state_snapshot(brain)) == []


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


def test_worker_uses_the_keyup_position(brain):
    """The brain verifies against the position at transmission START, not at
    parse time: the pilot reads their instruments while speaking, and the
    aircraft has moved on by the time Whisper finishes (see AGENTS-era saga:
    'bullseye 056' was judged against a position reached seconds later)."""
    spoken, log = [], []
    shared = _shared(brain, spoken, log)
    from ctr import AircraftTrack
    # The parse-time track would fail the bullseye check; the key-up track is
    # correct, so the check must use it.
    start = AircraftTrack(callsign="Colt 1", inside=True, lat=42.4, lon=42.4,
                          alt_ft=15000.0, distance_nm=40.0, relative="north",
                          heading=90.0)
    stingray = ControllerWorker(Controller.STINGRAY, 265_000_000, shared)
    stingray.start()
    stingray.submit("PilotA", b"Stingray, Colt 1, bullseye 056 for 36", 2.0,
                    start_track=start)
    stingray.queue.join()
    # With the parse-time track (None -> shared.track_for) the call could not
    # be verified; with the key-up snapshot it is acknowledged.
    pilot = brain.pilots["Colt 1"]
    assert pilot.phase == Phase.STINGRAY or "radar contact" in spoken[0][2]
    # A transmission WITHOUT a snapshot still works (falls back to live track).
    stingray.submit("PilotA", b"Stingray, Colt 1, help", 2.0)
    stingray.queue.join()
    assert len(spoken) == 2
