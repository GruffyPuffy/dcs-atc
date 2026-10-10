"""Identity resolution between SRS names and DCS player/unit names.

This is the tricky part for missions like TTI where many client slots share a
callsign (several "Springfield11" aircraft) and where the SRS name need not
equal the DCS player name. The resolver must pick the *right* aircraft or
refuse; it must never bind to the wrong one.
"""

from state_client import (Aircraft, norm_identity, resolve_player_unit,
                          slot_callsign)


def _u(unit_name, player, lat=42.0, lon=42.0):
    """A live player unit (unit `name` = DCS unit name, `player` = player)."""
    return Aircraft(callsign=unit_name, player=player, type="F/A-18C",
                    lat=lat, lon=lon, alt_ft=1000, heading=0, coalition=2)


def test_norm_identity_case_and_separators_but_keeps_digits():
    # Separators/case are insignificant...
    assert norm_identity("Caveman") == "caveman"
    assert norm_identity("CAVEMAN_1") == "caveman1"     # _ -> nothing, digit kept
    assert norm_identity("caveman-1") == "caveman1"
    # ...but the trailing digit is significant: DCS uses it to tell two
    # *different* players apart ("Caveman" vs "Caveman-1").
    assert norm_identity("Caveman") != norm_identity("Caveman-1")


def test_slot_callsign_from_unit_name():
    assert slot_callsign("Springfield11") == "Springfield 1"
    assert slot_callsign("Colt1") == "Colt 1"
    assert slot_callsign("Springfield") == ""      # no digits
    assert slot_callsign("Pilot #130") == ""       # not a callsign shape


def test_resolves_when_srs_name_equals_player_name():
    units = [_u("Springfield11", "Caveman"), _u("Springfield12", "Viper")]
    got = resolve_player_unit(units, "Caveman")
    assert got is not None and got.player == "Caveman"


def test_resolves_named_duplicate_suffix():
    units = [_u("Colt11", "Caveman"), _u("Colt12", "Caveman-1")]
    got = resolve_player_unit(units, "Caveman_1")
    assert got is not None and got.callsign == "Colt12"


def test_ambiguous_player_name_refuses_to_guess():
    # Two units report the SAME player name (should not normally happen, but we
    # must not hazard a guess if it does).
    units = [_u("Springfield11", "Caveman", lat=42.0),
             _u("Springfield12", "Caveman", lat=43.0)]
    assert resolve_player_unit(units, "Caveman") is None


def test_resolves_by_learned_callsign_when_player_name_differs():
    # SRS name "Bob" (no match), but the brain already learned Bob == Springfield 1-1
    # and a unit's player field is set to the callsign.
    units = [_u("Springfield11", "Springfield 1-1")]
    got = resolve_player_unit(units, "Bob", learned_callsign="Springfield 1-1")
    assert got is not None and got.player == "Springfield 1-1"


def test_resolves_by_slot_callsign_when_names_differ():
    # SRS name is the slot callsign "Springfield11"; the unit name is the same
    # but its player name is something else entirely.
    units = [_u("Springfield11", "Bob")]
    got = resolve_player_unit(units, "Springfield11")
    assert got is not None and got.callsign == "Springfield11"


def test_resolves_by_srs_position_among_shared_slots():
    # Two slots share the "Springfield11" callsign, players differ from SRS name,
    # but the SRS client reported a position -> match the nearest unit.
    units = [_u("Springfield11", "Alice", lat=42.00, lon=42.00),
             _u("Springfield12", "Bob", lat=43.00, lon=43.00)]
    got = resolve_player_unit(units, "Caveman", speaker_pos=(43.0005, 43.0005))
    assert got is not None and got.player == "Bob"


def test_solo_fallback_only_when_one_unit():
    units = [_u("Springfield11", "SomeoneElse")]
    assert resolve_player_unit(units, "Caveman", solo=True) is units[0]
    # ... but not when there are several candidates.
    assert resolve_player_unit(units + [_u("Colt11", "Other")], "Caveman",
                              solo=False) is None


def test_unresolvable_returns_none():
    units = [_u("Springfield11", "Alice"), _u("Colt11", "Bob")]
    assert resolve_player_unit(units, "Caveman") is None
