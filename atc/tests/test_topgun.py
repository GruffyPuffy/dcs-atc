"""Tests for the TopGun easter egg (see topgun.py).

The gag must be invisible unless ATC_TOPGUN is set, must never alter a pilot's
ATC state, and the drop-in clip lookup must be predictable.
"""

from __future__ import annotations

from pathlib import Path

import topgun
from brain import AtcBrain, Controller


# ---------- intent detection ----------

def test_spike_matches_the_stunt_requests():
    for text in [
        "Kutaisi Tower, Colt 1, request to bust the tower",
        "Tower, this is Ghost Rider requesting a flyby",
        "Colt 1, request flyby",
        "Colt 1 requesting low pass the tower",
        "Colt 1, buzz the tower",
        "Colt 1, beat up the field",
        "Colt 1, let's bomb the tower",
    ]:
        assert topgun.spike(text), text


def test_spike_tolerates_stt_mangling_of_bust():
    # STT often hears "bust" as "Bosto"/"Bost"/"busted" — the gag must still
    # fire, or a garbled request is just "say again" and the egg never triggers.
    for text in [
        "Chevy 1, request to Bosto Tower.",
        "Chevy One, Bust Tower.",
        "Chevy 1, request to Bost Tower",
        "Chevy 1, request to busted tower",
        "Chevy 1, request to bustin' the tower",
    ]:
        assert topgun.spike(text), text


def test_spike_ignores_normal_calls():
    for text in [
        "Colt 1, request taxi to runway 25",
        "Colt 1, inbound 35 miles north at angels 12",
        "Colt 1, request vectors for runway 25",
        "Colt 1, ready for departure",
        "Colt 1, request bearing and distance to Entry East",
        "",  # no text
    ]:
        assert not topgun.spike(text), text


def test_stall_matches_known_oneliners():
    assert topgun.stall("too close for missiles, switching to guns")
    assert topgun.stall("talk to me goose")
    assert not topgun.stall("Colt 1, request taxi")


# ---------- gating (the clip's presence is the switch) ----------

def test_disabled_without_clip(tmp_path, monkeypatch):
    # An isolated, empty search path and no marker: the gag is off.
    monkeypatch.setattr(topgun, "_wav_dirs", lambda: [tmp_path])
    monkeypatch.setattr(topgun, "_marker", lambda: None)
    assert topgun.negative_clip() is None
    assert topgun.enabled() is False


def test_enabled_with_clip(tmp_path, monkeypatch):
    (tmp_path / "topgun_negative.wav").write_bytes(b"RIFF....")
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    assert topgun.negative_clip() == (tmp_path / "topgun_negative.wav")
    assert topgun.enabled() is True


def test_enabled_by_marker_tts_only(tmp_path, monkeypatch):
    # A `topgun.txt` marker enables the gag with no clips (TTS only).
    monkeypatch.setattr(topgun, "_wav_dirs", lambda: [tmp_path])
    monkeypatch.setattr(topgun, "_marker", lambda: tmp_path / "topgun.txt")
    assert topgun.negative_clip() is None
    assert topgun.enabled() is True


def test_mode_precedence(tmp_path, monkeypatch):
    # wav wins, then txt, then off.
    clip = tmp_path / "topgun_negative.wav"
    monkeypatch.setattr(topgun, "_wav_dirs", lambda: [tmp_path])
    marker = tmp_path / "topgun.txt"

    monkeypatch.setattr(topgun, "_marker", lambda: None)
    assert topgun.mode() == "off"

    monkeypatch.setattr(topgun, "_marker", lambda: marker)
    assert topgun.mode() == "tts"

    clip.write_bytes(b"RIFF....")
    assert topgun.mode() == "wav"  # clip beats the marker
    assert topgun.enabled() is True


# ---------- clip lookup ----------

def test_clip_found_in_explicit_dir(tmp_path, monkeypatch):
    (tmp_path / "topgun_negative.wav").write_bytes(b"RIFF....")
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    found = topgun.clip("topgun_negative")
    assert found == (tmp_path / "topgun_negative.wav")


def test_clip_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    # No file written: the search dirs cannot contain topgun_bust.wav unless a
    # user dropped one in, which is not the case for an isolated tmp dir.
    assert topgun.clip("topgun_nonexistent_slot") is None


# ---------- brain integration (side-channel only) ----------

def test_brain_denies_and_arms(tmp_path, monkeypatch):
    (tmp_path / "topgun_negative.wav").write_bytes(b"RIFF....")
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    brain = AtcBrain()
    reply = brain.handle("Kutaisi Tower, Colt 1, request to bust the tower",
                         controller=Controller.TOWER, speaker="stefan")
    assert reply == topgun.NEGATIVE_LINE
    assert brain.topgun_armed("Colt 1")
    # No ATC phase change: the pilot is still Idle.
    assert brain.pilots["Colt 1"].phase.value == "Idle"


def test_brain_does_not_arm_without_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(topgun, "_wav_dirs", lambda: [tmp_path])
    monkeypatch.setattr(topgun, "_marker", lambda: None)
    brain = AtcBrain()
    reply = brain.handle("Kutaisi Tower, Colt 1, request to bust the tower",
                         controller=Controller.TOWER, speaker="stefan")
    assert reply != topgun.NEGATIVE_LINE
    assert not brain.topgun_armed("Colt 1")


def test_brain_normal_flow_unaffected(tmp_path, monkeypatch):
    (tmp_path / "topgun_negative.wav").write_bytes(b"RIFF....")
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    brain = AtcBrain()
    reply = brain.handle("Colt 1, request taxi to runway 25",
                         controller=Controller.GROUND, speaker="stefan")
    assert "taxi" in reply.lower()
    assert not brain.topgun_armed("Colt 1")


def test_brain_ignores_spike_on_ground(tmp_path, monkeypatch):
    # You ask the tower, not the ramp: a "bust the tower" request on Ground is
    # just "say again" and must NOT arm the gag (it should not be too easy).
    (tmp_path / "topgun_negative.wav").write_bytes(b"RIFF....")
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    brain = AtcBrain()
    reply = brain.handle("Chevy 1, request to Bosto Tower",
                         controller=Controller.GROUND, speaker="stefan")
    assert reply != topgun.NEGATIVE_LINE
    assert not brain.topgun_armed("Chevy 1")


def test_brain_arms_spike_on_control(tmp_path, monkeypatch):
    # Control is a valid frequency for the request (as is Tower).
    (tmp_path / "topgun_negative.wav").write_bytes(b"RIFF....")
    monkeypatch.setenv("ATC_TOPGUN_AUDIO", str(tmp_path))
    brain = AtcBrain()
    reply = brain.handle("Chevy 1, request to Bosto Tower",
                         controller=Controller.CONTROL, speaker="stefan")
    assert reply == topgun.NEGATIVE_LINE
    assert brain.topgun_armed("Chevy 1")


# ---------- bust timer (close + low + fast, held) ----------

def test_bust_fires_on_first_in_window_sample():
    # The monitor ticks every 2 s; a pass through a 200 m cylinder is one
    # sample. Default hold is 0 so that sample fires immediately.
    assert topgun.BUST_HOLD_S == 0.0
    w = topgun.BustWatch()
    assert w.update("Colt 1", near=True, low=True, fast=True, now=0.0) is True
    assert w.update("Colt 1", near=True, low=True, fast=True, now=2.0) is False


def test_bust_fires_after_hold():
    w = topgun.BustWatch(hold_s=1.5)
    assert w.update("Colt 1", near=True, low=True, fast=True, now=0.0) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=1.0) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=1.6) is True
    # Fires at most once.
    assert w.update("Colt 1", near=True, low=True, fast=True, now=9.0) is False


def test_bust_timer_starts_only_when_all_three_true():
    # Wide/high entry, then dive in: the clock must start at the dive, not on
    # entry, so a single late low+fast sample does not fire immediately.
    w = topgun.BustWatch(hold_s=1.5)
    assert w.update("Colt 1", near=True, low=False, fast=True, now=0.0) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=5.0) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=6.0) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=6.6) is True


def test_bust_timer_resets_when_condition_breaks():
    w = topgun.BustWatch(hold_s=1.5)
    assert w.update("Colt 1", near=True, low=True, fast=True, now=0.0) is False
    # Drops out of the window (e.g. climbed): the timer resets...
    assert w.update("Colt 1", near=True, low=False, fast=True, now=1.0) is False
    # ...so a later pass must hold the full duration again.
    assert w.update("Colt 1", near=True, low=True, fast=True, now=2.0) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=2.9) is False
    assert w.update("Colt 1", near=True, low=True, fast=True, now=3.6) is True


def test_bust_reset_rearms():
    w = topgun.BustWatch(hold_s=0.0)
    assert w.update("Colt 1", near=True, low=True, fast=True, now=0.0) is True
    w.reset("Colt 1")
    assert w.update("Colt 1", near=True, low=True, fast=True, now=0.0) is True


# Kutaisi tower (the live miss was 172 m CPA between two 2 s samples).
_TWR = (42.1797, 42.48089)


def _offset(lat, lon, north_m, east_m):
    import math
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def test_segment_distance_at_endpoint_is_zero():
    lat, lon = _TWR
    assert topgun.segment_distance_m(lat, lon, lat, lon, lat, lon) < 1.0


def test_segment_crosses_tower():
    a = _offset(*_TWR, 0, 300)
    b = _offset(*_TWR, 0, -300)
    assert topgun.segment_distance_m(a[0], a[1], b[0], b[1], *_TWR) < 20.0


def test_observe_fires_when_samples_straddle_cylinder():
    # Live miss: 375 m then 264 m, both ~108 ft AGL / 525 kt, CPA 172 m.
    w = topgun.BustWatch()
    a = _offset(*_TWR, 0, 375)
    b = _offset(*_TWR, 0, -264)
    assert w.observe("Colt 1", a[0], a[1], 108, 525, *_TWR, now=0.0) is False
    assert w.observe("Colt 1", b[0], b[1], 108, 525, *_TWR, now=2.0) is True


def test_observe_does_not_fire_high_flyover():
    w = topgun.BustWatch()
    a = _offset(*_TWR, 0, 375)
    b = _offset(*_TWR, 0, -264)
    assert w.observe("Colt 1", a[0], a[1], 15_000, 525, *_TWR, now=0.0) is False
    assert w.observe("Colt 1", b[0], b[1], 15_000, 525, *_TWR, now=2.0) is False


def test_observe_does_not_fire_slow():
    w = topgun.BustWatch()
    a = _offset(*_TWR, 0, 375)
    b = _offset(*_TWR, 0, -264)
    assert w.observe("Colt 1", a[0], a[1], 108, 180, *_TWR, now=0.0) is False
    assert w.observe("Colt 1", b[0], b[1], 108, 180, *_TWR, now=2.0) is False


def test_observe_fires_on_first_sample_inside_cylinder():
    w = topgun.BustWatch()
    lat, lon = _TWR
    assert w.observe("Colt 1", lat, lon, 108, 525, *_TWR, now=0.0) is True


def test_observe_reset_clears_previous_sample():
    w = topgun.BustWatch()
    a = _offset(*_TWR, 0, 375)
    w.observe("Colt 1", a[0], a[1], 108, 525, *_TWR, now=0.0)
    w.reset("Colt 1")
    b = _offset(*_TWR, 0, -264)
    # Without the previous sample the 264 m point is outside the cylinder.
    assert w.observe("Colt 1", b[0], b[1], 108, 525, *_TWR, now=2.0) is False
