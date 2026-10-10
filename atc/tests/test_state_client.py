"""Tests for StateClient's thin bridge wrappers (no live DCS needed)."""

from __future__ import annotations

from state_client import StateClient


def test_message_sends_text_and_player(monkeypatch):
    client = StateClient()
    seen = {}

    def fake_exchange(op, **fields):
        seen["op"] = op
        seen.update(fields)
        return {"ok": True}

    monkeypatch.setattr(client, "exchange", fake_exchange)
    assert client.message("RX Colt 1: request taxi", player="stefan") is True
    assert seen["op"] == "message"
    assert seen["text"] == "RX Colt 1: request taxi"
    assert seen["player"] == "stefan"


def test_message_returns_false_on_bridge_error(monkeypatch):
    client = StateClient()
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": False, "error": "NO_MESSAGE_API"})
    assert client.message("hello") is False


def test_tower_returns_none_on_error(monkeypatch):
    client = StateClient()
    monkeypatch.setattr(client, "exchange",
                        lambda op, **fields: {"ok": False, "error": "NO_AIRBASE"})
    assert client.tower("Kutaisi") is None
