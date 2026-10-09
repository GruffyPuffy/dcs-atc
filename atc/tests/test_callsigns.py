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


def test_recovers_multiword_fracture(callsigns):
    # Real STT error observed live: "spring fail" for "Springfield".
    assert callsigns.extract("Spring fail 1-1, on final") == "Springfield 1-1"


def test_fuzzy_recovers_unknown_mishearing():
    # A mishearing outside the generated rules, recovered by edit distance.
    reg = CallsignRegistry(["Springfield", "Colt", "Enfield", "Viper"])
    assert reg.extract("Springfeld 2, inbound") == "Springfield 2"


def test_fuzzy_requires_a_number():
    # A bare misheard word with no flight number must NOT become a callsign.
    reg = CallsignRegistry(["Springfield", "Colt"])
    assert reg.extract("springing into action") is None


def test_looks_like_ignores_content_opening_readback(callsigns):
    # A readback that opens with content (no callsign) must not "look like"
    # the pilot's callsign — so it is never silently attributed to them.
    assert not callsigns.looks_like(
        "Cleared taxi Sierra Echo and hold short runway 25", "Springfield 1-1")


def test_looks_like_matches_garbled_names(callsigns):
    assert callsigns.looks_like("Spring fail 1-1", "Springfield 1-1")
    assert callsigns.looks_like("springPill", "Springfield 2")


def test_speaker_resolution_prefers_known_and_resembling_pilot():
    from brain import AtcBrain, Controller
    reg = CallsignRegistry(["Springfield", "Colt"])
    brain = AtcBrain(callsigns=reg)
    brain.set_pilot_count(1)
    brain.set_active_pilots(["Springfield 1-1"])
    brain.remember_speaker("Caveman", "Springfield 1-1")
    reply = brain.handle("Spring fail 1-1 on final",
                         controller=Controller.TOWER, speaker="Caveman")
    assert reply and "cleared to land" in reply.lower()
    assert brain.pilots["Springfield 1-1"].phase.value == "Landing"


def test_speaker_resolution_not_applied_to_content_opening_calls():
    from brain import AtcBrain, Controller
    reg = CallsignRegistry(["Springfield", "Colt"])
    brain = AtcBrain(callsigns=reg)
    brain.set_pilot_count(1)
    brain.set_active_pilots(["Springfield 1-1"])
    brain.remember_speaker("Caveman", "Springfield 1-1")
    # No callsign, no resemblance -> the solo "say again with your callsign".
    reply = brain.handle("Cleared taxi Sierra Echo and hold short runway 25",
                         controller=Controller.GROUND, speaker="Caveman")
    assert "say again" in reply.lower()


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
