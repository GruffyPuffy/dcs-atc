"""Tests for StateClient's thin bridge wrappers (no live DCS needed)."""

from __future__ import annotations

from state_client import StateClient


def test_message_sends_text_and_player(monkeypatch):
    client = StateClient()
    seen = {}

    def fake_exchange(op, **fields):
        seen["op"] = op
        seen.update(fields)
        return {"ok": True, "result": {"sent": "player"}}

    monkeypatch.setattr(client, "exchange", fake_exchange)
    assert client.message("RX Colt 1: request taxi", player="stefan") is True
    assert seen["op"] == "message"
    assert seen["text"] == "RX Colt 1: request taxi"
    assert seen["player"] == "stefan"


def test_mission_message_is_an_opt_in_targeted_variant(monkeypatch):
    client = StateClient()
    seen = {}

    def fake_exchange(op, **fields):
        seen["op"] = op
        seen.update(fields)
        return {"ok": True, "result": {"sent": "mission"}}

    monkeypatch.setattr(client, "exchange", fake_exchange)
    assert client.message("19:32:22 - YOU (Ch 7): \"hello\"",
                         player="stefan", duration=20.0,
                         mission=True) is True
    assert seen["op"] == "message"
    assert seen["mission"] is True
    assert seen["player"] == "stefan"
    assert seen["duration"] == 20.0

    # It must not treat a chat/broadcast fallback as a mission delivery.
    monkeypatch.setattr(client, "exchange", lambda op, **fields: {
        "ok": True, "result": {"sent": "all"}})
    assert client.message("private", player="stefan", mission=True) is False


def test_message_returns_false_on_bridge_error(monkeypatch):
    client = StateClient()
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": False, "error": "NO_MESSAGE_API"})
    assert client.message("hello") is False


def test_message_named_player_falls_back_to_broadcast_is_false(monkeypatch):
    # A named send that the bridge could only broadcast (player not in the
    # mission yet) must report False, so the join greeting retries.
    client = StateClient()
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": True, "result": {"sent": "all"}})
    assert client.message("welcome", player="stefan") is False
    # A confirmed per-player delivery is True.
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": True, "result": {"sent": "player"}})
    assert client.message("welcome", player="stefan") is True


def test_tower_returns_none_on_error(monkeypatch):
    client = StateClient()
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": False, "error": "NO_AIRBASE"})
    assert client.tower("Kutaisi") is None


def test_bullseye_returns_point(monkeypatch):
    client = StateClient()
    seen = {}

    def fake_exchange(op, **fields):
        seen["op"] = op
        seen.update(fields)
        return {"ok": True, "result": {"coalition": 2, "lat": 42.1, "lon": 42.5}}

    monkeypatch.setattr(client, "exchange", fake_exchange)
    point = client.bullseye()
    assert point["lat"] == 42.1 and point["lon"] == 42.5
    assert seen["op"] == "bullseye"
    assert seen["coalition"] == 2  # blue


def test_bullseye_returns_none_when_unset(monkeypatch):
    # A mission with no bullseye must degrade to None, never a made-up point.
    client = StateClient()
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": False, "error": "NO_BULLSEYE"})
    assert client.bullseye() is None


def test_weather_converts_dcs_blow_to_direction_to_aviation_from(monkeypatch):
    client = StateClient()
    monkeypatch.setattr(client, "exchange", lambda op, **fields: {
        "ok": True,
        "result": {"wind_dir": 30, "wind_dir_2000": 180,
                   "wind_speed_ms": 4},
    })

    weather = client.weather()

    assert weather["wind_dir"] == 210
    assert weather["wind_dir_2000"] == 0
    assert weather["wind_speed_ms"] == 4
