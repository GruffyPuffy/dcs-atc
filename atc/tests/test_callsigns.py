"""Callsign recognition: mission names, STT variants, number words."""

from callsigns import CallsignRegistry


def test_extracts_plain_callsign(callsigns):
    assert callsigns.extract("Colt 1, requesting taxi") == "Colt 1"


def test_extracts_number_word(callsigns):
    assert callsigns.extract("Colt one, ready") == "Colt 1"


def test_extracts_flight_element(callsigns):
    assert callsigns.extract("Colt 1-2 checking in") == "Colt 1-2"


def test_tolerates_stt_variant(callsigns):
    # "cold" is a generated variant of "Colt"
    assert callsigns.extract("cold one, inbound") == "Colt 1"


def test_canonicalises_mission_spelling(callsigns):
    # mission slot "Springfield21" -> flight name "Springfield"
    assert callsigns.extract("Springfield 2, with you") == "Springfield 2"


def test_no_callsign_returns_none(callsigns):
    assert callsigns.extract("requesting taxi to the ramp") is None


def test_number_word_not_matched_as_name(callsigns):
    # "four two" must NOT be read as the flight "Ford"
    assert callsigns.extract("four two, holding short") is None


def test_from_mission_strips_trailing_digits():
    reg = CallsignRegistry.from_mission([{"name": "Springfield21"},
                                         {"name": "Colt1"}])
    assert "Springfield" in reg.names
    assert "Colt" in reg.names


def test_from_mission_empty_falls_back_to_defaults():
    reg = CallsignRegistry.from_mission([])
    assert reg.names  # DEFAULT_NAMES
