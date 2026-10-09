"""Spoken-number normalisation for TTS (aviation digit-by-digit)."""

from speech import digit_words, spell_atc_numbers


def test_digit_words_uses_niner():
    assert digit_words("150") == "one five zero"
    assert digit_words("2992") == "two niner niner two"


def test_heading_is_digit_by_digit():
    assert spell_atc_numbers("heading 150") == "heading one five zero"
    assert spell_atc_numbers("heading 074") == "heading zero seven four"


def test_two_digit_heading_is_zero_padded():
    # A heading is always three digits: "60" is "zero six zero", not "sixty".
    assert spell_atc_numbers("heading 60") == "heading zero six zero"
    assert spell_atc_numbers("heading 9") == "heading zero zero niner"


def test_wind_is_digit_by_digit():
    # The speed ("at 5") stays a whole value.
    assert spell_atc_numbers("wind 270 at 5") == "wind two seven zero at 5"


def test_runway_is_digit_by_digit():
    assert spell_atc_numbers("runway 25") == "runway two five"
    assert spell_atc_numbers("hold short runway 07") == "hold short runway zero seven"
    # A single-digit runway end is zero-padded too.
    assert spell_atc_numbers("runway 7") == "runway zero seven"


def test_qnh_four_digit_and_decimal():
    assert spell_atc_numbers("QNH 2992") == "QNH two niner niner two"
    assert spell_atc_numbers("QNH 29.92") == "QNH two niner niner two"


def test_callsign_dash_is_silent():
    assert spell_atc_numbers("Colt 1-1") == "Colt 1 1"
    # Multiple elements / flight+element forms.
    assert spell_atc_numbers("Colt 1-1, cleared") == "Colt 1 1, cleared"


def test_leaves_whole_value_numbers_alone():
    # These are read as whole values and must NOT be spelled out.
    assert spell_atc_numbers("1500 ft or below") == "1500 ft or below"
    assert spell_atc_numbers("Angels 15") == "Angels 15"
    assert spell_atc_numbers("contact Tower on channel 7") == "contact Tower on channel 7"
    assert spell_atc_numbers("elevation 69 feet") == "elevation 69 feet"


def test_full_atc_line():
    out = spell_atc_numbers(
        "Colt 1-1, Tower, readback correct, wind 270 at 5, runway 25, "
        "cleared for takeoff.")
    assert out == ("Colt 1 1, Tower, readback correct, wind two seven zero at 5, "
                   "runway two five, cleared for takeoff.")
