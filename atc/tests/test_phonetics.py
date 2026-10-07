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
