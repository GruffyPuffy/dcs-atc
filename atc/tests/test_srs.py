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


def test_normal_transmission_waits_until_channel_gap_expires():
    c = _client("tower", set())
    freq = 250_000_000
    c._record_tx_complete(freq, 2.0, now=10.0)
    c._tx_queue.append((freq, [b"ordinary"], 2.0, False))

    assert c._next_tx_item(now=11.99) is None
    assert c._next_tx_item(now=12.0) == (freq, [b"ordinary"], 2.0, False)


def test_answer_window_is_a_single_requested_wait_not_added_to_normal():
    c = _client("tower", set())
    freq = 250_000_000
    c._record_tx_complete(freq, 5.0, now=10.0)
    c._tx_queue.append((freq, [b"answer"], 5.0, False))

    assert c._next_tx_item(now=14.99) is None
    assert c._next_tx_item(now=15.0) is not None


def test_pilot_transmission_cancels_remaining_wait_then_lbt_releases_queue():
    c = _client("tower", set())
    freq = 250_000_000
    c._record_tx_complete(freq, 5.0, now=10.0)
    c._tx_queue.append((freq, [b"queued ATC"], 2.0, False))

    # Activity cancels the outstanding answer timeout, but ATC remains blocked
    # for as long as SRS considers the pilot's receive stream active.
    c._stream_start[freq] = 11.0
    c._cancel_tx_wait(freq)
    assert c._next_tx_item(now=11.1) is None

    del c._stream_start[freq]  # SRS watchdog marks the receive stream ended
    assert c._next_tx_item(now=11.2) == (freq, [b"queued ATC"], 2.0, False)


def test_emergency_bypasses_wait_but_still_listens_to_pilot():
    c = _client("tower", set())
    freq = 250_000_000
    c._record_tx_complete(freq, 2.0, now=10.0)
    c._tx_queue.append((freq, [b"routine"], 2.0, False))
    c._tx_queue.append((freq, [b"emergency"], 2.0, True))

    c._stream_start[freq] = 12.0
    assert c._next_tx_item(now=10.1) is None
    assert len(c._tx_queue) == 2

    del c._stream_start[freq]
    assert c._next_tx_item(now=10.1) == (freq, [b"emergency"], 2.0, True)
    c._record_tx_complete(freq, 2.0, now=10.2)
    assert c._next_tx_item(now=12.19) is None
    assert c._next_tx_item(now=12.2) == (freq, [b"routine"], 2.0, False)
