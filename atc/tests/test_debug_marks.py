import datetime as dt
import json

from debug_marks import DebugMarkWriter, parse_debug_mark


def test_debug_marker_accepts_bare_prefix_or_note():
    assert parse_debug_mark("DEBUG") == ""
    assert parse_debug_mark(" debug: I saw bla bla ") == "I saw bla bla"
    assert parse_debug_mark("DEBUG - point marked") == "point marked"
    assert parse_debug_mark("debugging the radio") is None
    assert parse_debug_mark("Ground, Colt 1, debug") is None


def test_debug_writer_records_session_version_hash_and_snapshot(tmp_path):
    started = dt.datetime(2026, 11, 1, 12, 30, tzinfo=dt.timezone.utc)
    writer = DebugMarkWriter(tmp_path, "Kutaisi", "v2026.11.01.2",
                             "0123456789abcdef", started_at=started)

    assert writer.append({"guid": "1A2B3C4D", "note": "point marked", "aircraft_picture": [
        {"callsign": "Colt11", "alt_ft": 2500},
    ]}) == 1
    assert writer.append({"note": "second marker"}) == 2

    records = [json.loads(line) for line in writer.path.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["session_id"] == "kutaisi-20261101T123000Z-0123456789ab"
    assert records[0]["atc_version"] == "v2026.11.01.2"
    assert records[0]["atc_git_sha"] == "0123456789abcdef"
    assert records[0]["guid"] == "1A2B3C4D"
    assert records[0]["aircraft_picture"][0]["callsign"] == "Colt11"
    assert records[1]["sequence"] == 2
