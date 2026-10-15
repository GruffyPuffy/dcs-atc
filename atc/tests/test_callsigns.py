"""Callsign recognition: mission names, STT variants, number words."""

from callsigns import CallsignRegistry, flight_name


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
    # The element-1 lead collapses to the flight callsign (see canonical).
    assert callsigns.extract("Spring fail 1-1, on final") == "Springfield 1"


def test_element_lead_collapses_to_flight_callsign(callsigns):
    # The lead says "Springfield 1" or "Springfield 1-1" — same pilot, one key.
    assert callsigns.extract("Springfield 1-1, ready") == "Springfield 1"


def test_comma_between_name_and_number(callsigns):
    # STT punctuates the callsign at a pause ("Ground, Springfield, 11").
    assert callsigns.extract("Ground, Springfield, 11") == "Springfield 1"
    assert callsigns.extract("Ground, Springfield, 1-1") == "Springfield 1"


def test_comma_does_not_swallow_formation_count(callsigns):
    # A comma before a *word* number must not become an element ("Colt 1-2").
    assert callsigns.extract(
        "Colt 1, two-ship Hornets on Ramp South") == "Colt 1"


def test_extract_all_lists_every_flight_named(callsigns):
    # A readback naming an altitude (Ford 15 = heading 150 toward Ford) and the
    # sender lists both, in the order they appear.
    assert callsigns.extract_all("Climbing to Ford 15, Springfield 1") == [
        "Ford 1-5", "Springfield 1"
    ]
    assert callsigns.extract("Springfield 1, ready") == "Springfield 1"
    # A wingman (element 2+) stays distinct.
    assert callsigns.extract("Springfield 1-2, ready") == "Springfield 1-2"


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
    brain.set_active_pilots(["Springfield 1"])
    brain.remember_speaker("Caveman", "Springfield 1")
    reply = brain.handle("Spring fail 1-1 on final",
                         controller=Controller.TOWER, speaker="Caveman")
    assert reply and "cleared to land" in reply.lower()
    assert brain.pilots["Springfield 1"].phase.value == "Landing"


def test_known_speaker_attributed_even_for_content_opening_calls():
    from brain import AtcBrain, Controller
    reg = CallsignRegistry(["Springfield", "Colt"])
    brain = AtcBrain(callsigns=reg)
    brain.set_pilot_count(1)
    brain.set_active_pilots(["Springfield 1"])
    brain.remember_speaker("Caveman", "Springfield 1")
    # A readback that opens with content and names no callsign is still
    # attributed to the known speaker (we know who is on the radio), so the
    # pilot is not forced to repeat just to satisfy STT.
    reply = brain.handle("Cleared taxi Sierra Echo and hold short runway 25",
                         controller=Controller.GROUND, speaker="Caveman")
    assert reply and "Springfield 1" in reply


def test_unknown_speaker_content_opening_call_is_not_guessed():
    from brain import AtcBrain, Controller
    reg = CallsignRegistry(["Springfield", "Colt"])
    brain = AtcBrain(callsigns=reg)
    brain.set_pilot_count(1)
    brain.set_active_pilots(["Springfield 1"])
    # No learned speaker and no resemblance -> the solo "say again" prompt.
    reply = brain.handle("Cleared taxi Sierra Echo and hold short runway 25",
                         controller=Controller.GROUND, speaker="Stranger")
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


def test_add_learns_a_dynamic_slot_callsign():
    """A dynamic slot is not in env.mission, so the registry must learn it.

    The mission only defines Hornet/Uzi; the player spawned into a dynamically
    created "Chevy 11" slot, which the live unit list reports. Without `add`
    the registry never knows "Chevy" and every call gets "say again".
    """
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"},
                                         {"name": "Uzi11"}])
    assert "Chevy" not in reg.names

    added = reg.add(["Chevy 11"])
    assert added == ["Chevy"]
    assert "Chevy" in reg.names
    # The new name is fully usable: extraction and fuzzy matching both work.
    assert reg.extract_all("ground, Chevy one one") == ["Chevy 1"]
    assert reg.looks_like("chevy one one", "Chevy")


def test_add_is_idempotent_and_ignores_known_names():
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"}])
    assert reg.add(["Hornet11"]) == []      # already known
    assert reg.add(["Chevy 11"]) == ["Chevy"]
    assert reg.add(["Chevy 11"]) == []      # second time is a no-op
    assert reg.names.count("Chevy") == 1


def test_add_keeps_longest_name_first():
    """A longer name must still win over a shorter prefix of it."""
    reg = CallsignRegistry.from_mission([{"name": "Colt1"}])
    reg.add(["Coltrane 1"])
    assert reg.names[0] == "Coltrane"


def test_prune_drops_learned_names_but_keeps_mission_ones():
    """A departed player's dynamic callsign is forgotten; mission ones are not.

    Mission callsigns stay valid when the slot is empty, so they must survive a
    prune even though nobody is flying them.
    """
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"}])
    reg.add(["Chevy 11"])

    removed = reg.prune(["Hornet11"])  # Chevy's player left
    assert removed == ["Chevy"]
    assert "Chevy" not in reg.names
    assert "Hornet" in reg.names       # mission callsign retained
    # The pruned name is no longer recognised.
    assert reg.extract_all("ground, Chevy one one") == []


def test_prune_keeps_a_learned_name_while_its_player_is_online():
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"}])
    reg.add(["Chevy 11"])
    assert reg.prune(["Hornet11", "Chevy 11"]) == []
    assert "Chevy" in reg.names


def test_prune_is_a_noop_when_nothing_was_learned():
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"}])
    assert reg.prune([]) == []
    assert "Hornet" in reg.names


def test_flight_name_strips_the_flight_and_element_numbers():
    assert flight_name("Springfield 1-2") == "Springfield"
    assert flight_name("Springfield 1") == "Springfield"
    assert flight_name("Springfield11") == "Springfield"
    assert flight_name("Colt1") == "Colt"
    assert flight_name("") == ""


def test_add_reduces_any_element_to_one_flight_name():
    """Every element of a flight shares one flight name.

    The registry must not learn "Springfield 1-" from the second element's
    callsign, so adding any element is a no-op once the name is known.
    """
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"}])
    assert reg.add(["Springfield 1"]) == ["Springfield"]
    assert reg.add(["Springfield 1-2"]) == []
    assert reg.add(["Springfield12"]) == []
    assert reg.names.count("Springfield") == 1


def test_prune_keeps_a_learned_name_named_by_any_element():
    reg = CallsignRegistry.from_mission([{"name": "Hornet11"}])
    reg.add(["Springfield 1"])
    assert reg.prune(["Springfield 1-3"]) == []


def test_forward_one_is_ford_when_focused():
    """Whisper hears 'Ford' as 'forward'; only Ford's error list should fire."""
    reg = CallsignRegistry(["Ford", "Uzi", "Colt"])
    assert reg.extract("ground, forward one", focus=["Ford 1"]) == "Ford 1"
    assert reg.extract("ground, forward, one", focus=["Ford 1"]) == "Ford 1"
    # Without focus the same spelling is still Ford (it's in KNOWN_ERRORS),
    # but a competing flight must not win once we know the speaker.
    assert reg.extract("ground for the one", focus=["Ford 1"]) != "Uzi 1"


def test_focus_ignores_other_flights_errors():
    reg = CallsignRegistry(["Ford", "Uzi", "Dodge"])
    assert reg.extract("touch one", focus=["Ford 1"]) is None
    assert reg.extract("touch one", focus=["Dodge 1"]) == "Dodge 1"