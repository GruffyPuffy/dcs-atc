"""Spoken-number normalisation for TTS (aviation digit-by-digit).

Piper/espeak reads a bare number as a *whole value* — "heading 150" becomes "one
hundred fifty", "runway 25" becomes "twenty five", "QNH 2992" becomes "two
thousand nine hundred ninety two" — which is **not** how controllers read them.
Aviation reads headings, wind, runways and QNH digit-by-digit:

    heading 150  -> "one five zero"
    wind 270     -> "two seven zero"
    runway 25    -> "two five"
    QNH 2992     -> "two niner niner two"

and a callsign element is read digit-by-digit: "Colt 1-1" is "Colt one one",
never "Colt eleven" or "Colt one dash one".

`spell_atc_numbers(text)` rewrites those forms **for synthesis only**; the log
and the chatter keep the real, readable text. `speak()` applies it just before
synthesis, alongside the PRONUNCIATION map.
"""

from __future__ import annotations

import re

# How each digit is spoken. ICAO uses "niner" for 9 to avoid confusion over a
# poor radio; "three"/"four"/"five" stay as normal words (the ICAO "tree"/
# "fower"/"fife" read oddly and offer little benefit here). Tweak freely.
SPOKEN_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner",
}


def digit_words(digits: str) -> str:
    """Spell a run of digits individually: "150" -> "one five zero"."""
    return " ".join(SPOKEN_DIGITS.get(d, d) for d in digits)


# Keyword-anchored rewrites, so ONLY the numbers that are read digit-by-digit
# are changed. Altitudes ("1500 ft or below"), "Angels 15", channel numbers,
# temperatures, visibility, etc. are read as whole values and are left alone.
#
# Each numeric field is zero-padded to its fixed width first: a heading is always
# three digits ("heading 60" -> "heading 060" -> "zero six zero"), not "sixty".
_HEADING = re.compile(r"\bheading\s+(\d{1,3})\b", re.IGNORECASE)
_WIND = re.compile(r"\bwind\s+(\d{1,3})\b", re.IGNORECASE)
# QNH is read as four digits whether written "2992" (controllers) or "29.92"
# (ATIS): both -> "two niner niner two".
_QNH = re.compile(r"\bQNH\s+(\d{4}|\d{2}\.\d{2})\b", re.IGNORECASE)
_RUNWAY = re.compile(r"\brunway\s+(\d{1,2})\b", re.IGNORECASE)
# A digit-dash-digit is a callsign element ("Colt 2-1"): speak each digit.
_CALLSIGN_ELEMENT = re.compile(r"\b(\d+)-(\d+)\b")


def _qnh_digits(raw: str) -> str:
    return raw.replace(".", "")


# A segment the model itself flags as probably non-speech. A near-silent clip
# (a PTT click or a breath) can still decode to a short phrase — often a
# hallucination — but carries a high no_speech probability *and* a low average
# log probability. Both must hold, mirroring faster-whisper's own silence test,
# so a confident short transmission is never dropped.
NO_SPEECH_PROB = 0.6
NO_SPEECH_LOGPROB = -1.0


def is_probably_non_speech(segments) -> bool:
    """True if every segment looks like non-speech (a PTT click / breath).

    Used to drop a hallucinated transcript: Whisper can emit a short phrase for
    a near-silent clip, but flags it with a high `no_speech_prob` and a low
    `avg_logprob`. Requiring both keeps a confident short call. An empty segment
    list is *not* non-speech (there is simply nothing to judge).
    """
    return bool(segments) and all(
        getattr(s, "no_speech_prob", 0.0) > NO_SPEECH_PROB
        and getattr(s, "avg_logprob", 0.0) < NO_SPEECH_LOGPROB
        for s in segments)


def spell_atc_numbers(text: str) -> str:
    """Rewrite ATC numbers in `text` to their spoken, digit-by-digit form."""
    text = _HEADING.sub(
        lambda m: f"heading {digit_words(m.group(1).zfill(3))}", text)
    text = _WIND.sub(
        lambda m: f"wind {digit_words(m.group(1).zfill(3))}", text)
    text = _QNH.sub(lambda m: f"QNH {digit_words(_qnh_digits(m.group(1)))}", text)
    text = _RUNWAY.sub(
        lambda m: f"runway {digit_words(m.group(1).zfill(2))}", text)
    text = _CALLSIGN_ELEMENT.sub(
        lambda m: f"{digit_words(m.group(1))} {digit_words(m.group(2))}", text)
    return text
