"""SRS client: the per-client echo guard (multi-client self-hearing)."""

from __future__ import annotations

import srs_client
from srs_client import SrsClient


def _client(name: str, guids: set[str]) -> SrsClient:
    c = SrsClient("127.0.0.1", 5002, name, [250_000_000], coalition=2,
                  ignore_guids=guids)
    return c


def test_voice_from_ignored_guid_is_dropped(monkeypatch):
    """A bot client must not process another bot client's transmission.

    This is the echo-loop guard: without it the bot hears its own "ATC online"
    / ATIS broadcasts and answers them forever.
    """
    other = "AAAAAAAAAAAAAAAAAAAAAA"
    c = _client("Gudauta Tower tower", {other})
    monkeypatch.setattr(srs_client, "decode_voice_packet", lambda m: {
        "original_client_guid": other,
        "audio_part1_bytes": b"\x00" * 32,
        "frequencies": [250_000_000.0],
    })
    c._handle_voice(b"packet")
    assert c.audio[250_000_000] == []  # dropped before buffering


def test_ignore_guids_defaults_to_empty():
    c = SrsClient("127.0.0.1", 5002, "x", [250_000_000])
    assert c.ignore_guids == set()


def test_own_guids_all_ignored_across_clients():
    """Mirrors atc_bot wiring: every bot client ignores all bot guids."""
    clients = [SrsClient("127.0.0.1", 5002, n, [250_000_000])
               for n in ("tower", "ground", "control", "atis")]
    own = {c.guid for c in clients}
    for c in clients:
        c.ignore_guids = set(own)
    for c in clients:
        assert own <= c.ignore_guids         # ignores every bot guid
        assert "PILOTGUID" not in c.ignore_guids  # but not a real pilot
