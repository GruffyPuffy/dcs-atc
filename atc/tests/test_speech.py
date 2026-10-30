"""Spoken-number normalisation for TTS (aviation digit-by-digit)."""

from speech import digit_words, is_probably_non_speech, spell_atc_numbers


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


def test_callsign_element_digits_are_spoken_separately():
    assert spell_atc_numbers("Colt 1-1") == "Colt one one"
    assert spell_atc_numbers("Colt 2-1") == "Colt two one"
    assert spell_atc_numbers("Colt 21-1") == "Colt two one one"
    assert spell_atc_numbers("Colt 1-1, cleared") == "Colt one one, cleared"


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
    assert out == ("Colt one one, Tower, readback correct, wind two seven zero at 5, "
                   "runway two five, cleared for takeoff.")


class _Seg:
    """Minimal stand-in for a faster-whisper Segment (only the fields we read)."""

    def __init__(self, text: str, no_speech_prob: float, avg_logprob: float = 0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


def test_non_speech_guard_drops_a_hallucinated_click():
    # A PTT click / breath: the model emits a phrase but flags it non-speech
    # with a low confidence.
    assert is_probably_non_speech(
        [_Seg("Call signs and standard phraseology.", 0.9, avg_logprob=-1.5)])


def test_non_speech_guard_keeps_a_confident_short_call():
    # High no_speech_prob but a confident decode: a real short transmission.
    assert not is_probably_non_speech(
        [_Seg("Tower, Colt 1", 0.9, avg_logprob=-0.2)])


def test_non_speech_guard_keeps_real_speech():
    assert not is_probably_non_speech(
        [_Seg("Tower, Colt 1, ready for departure", 0.05, avg_logprob=-0.3)])


def test_non_speech_guard_keeps_a_mixed_transmission():
    # One confident segment is enough to keep the whole transcript.
    segs = [_Seg("Tower, Colt 1", 0.9, avg_logprob=-1.5),
            _Seg("ready for departure", 0.1, avg_logprob=-0.3)]
    assert not is_probably_non_speech(segs)


def test_non_speech_guard_ignores_an_empty_transcript():
    # No segments means "nothing decoded", not "non-speech".
    assert not is_probably_non_speech([])
