"""Phonetic variant generation."""

from phonetics import generate_variants


def test_includes_original_first():
    variants = generate_variants("Colt")
    assert variants[0] == "colt"


def test_generates_known_confusions():
    variants = generate_variants("Colt")
    # t<->d, o<->u, plus known whole-word errors
    assert "cold" in variants
    assert "cult" in variants


def test_registry_filters_number_word_variants():
    # "four" (a variant of "Ford") is a number word; the registry must drop it
    # so "four two" is not read as the flight "Ford".
    from callsigns import CallsignRegistry
    reg = CallsignRegistry(["Ford"])
    assert "four" not in reg.variants["ford"]


def test_respects_min_length():
    variants = generate_variants("Uzi", min_length=3)
    assert all(len(v) >= 3 for v in variants)


def test_empty_name():
    assert generate_variants("") == []


def test_stt_hint_puts_callsigns_first():
    from phonetics import stt_hint
    hint = stt_hint(["Springfield", "Colt"], ["Gudauta"])
    # callsigns dominate the front of the hint (highest-value tokens)
    assert hint.startswith("Springfield, Colt")
    assert "Gudauta" in hint
    assert "cleared for takeoff" in hint


def test_stt_hint_dedupes_and_caps():
    from phonetics import stt_hint
    hint = stt_hint(["Colt", "colt", "Colt"], ["Kutaisi", "kutaisi"])
    assert hint.lower().count("colt") == 1
    assert hint.lower().count("kutaisi") == 1
    assert len(stt_hint(["X" * 100] * 50, max_chars=100)) <= 100
