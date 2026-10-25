"""The coach: wording suggestions for calls whose INTENT was already handled.

This layer sits **after** intent recognition and never changes it: the brain
resolves the call exactly as before, and the coach only asks "was that the
SOP wording?" It is a post-step on utterance TEXT, deliberately in its own
module (`coach.py`) so the state machine is untouched.

- **Coach mode** (default): the brain appends a short spoken suggestion when a
  call's intent is handled but the phrasing deviates from the Master Arms SOP.
- **Casual mode**: the brain never asks; the coach is silent.

Data: the `Rules` list below. Each rule matches a *sloppy* wording variant and
names the canonical pilot call from the MA SOP (same dialogs the kneeboard
renders). Nothing is required for the call to work — the suggestion is pure
pedagogy on top. Add rules as new sloppy habits show up in debriefs.

The suggestion NEVER fires when the reply already carries semantic feedback
("readback incomplete"): one corrective note per transmission, no nagging.
"""

import re


class Rule:
    """One sloppy wording -> canonical call mapping."""

    def __init__(self, sloppy: re.Pattern[str], proper: str,
                 unless: re.Pattern[str] | None = None):
        self.sloppy = sloppy
        self.proper = proper  # the canonical SOP wording to suggest
        # Words that, anywhere in the call, make it proper already.
        self.unless = unless


# Deliberately small for v1 — only demonstrated habits from debriefs:
_RULES: list[Rule] = [
    # "give me taxi", "I need taxi", "can I have taxi"... — the SOP call is
    # "requesting taxi" (TWR/GND: "Ground, Adder 11, requesting taxi").
    Rule(
        sloppy=re.compile(
            r"\b(?:give me|i need|i want|can i (?:get|have)|let me have)\b"
            r"[^.]*\btaxi\b"),
        proper="requesting taxi",
        unless=re.compile(r"\brequest(?:ing|s)?\b[^.]*\btaxi\b")),
    # "ready for takeoff" — MA SOP says "ready for departure"; takeoff is the
    # clearance the TOWER gives ("cleared takeoff"), not what the pilot asks.
    Rule(
        sloppy=re.compile(r"\bready (?:for takeoff|to go)\b"),
        proper="ready for departure"),
]

# Duplicate canonical calls are already filtered by the brain; a note is
# suppressed there when the reply already carries semantic feedback.
# MA SOP readbacks run content-first and END with the callsign:
#   "Cleared taxi Sierra Echo and hold short Runway 25, Adder11."
# The lead-callsign readback note is built per call in `note_readback` below.


def note_readback(utterance: str, callsign: str) -> str:
    """The readback-specific wording note, or "" (called on readbacks only).

    The brain only asks when the transmission was judged as a readback (it set
    `pilot.last_readback`), so a normal "Ground, Colt 1, requesting taxi" —
    which also starts with the callsign — can never trip this. Fires when the
    READBACK opens with the callsign (STT variant of their name + number is
    attempted verbatim; a mangled name just means no note, silence is cheap).
    """
    if not callsign or not utterance:
        return ""
    low_cs = callsign.lower().strip()
    # The callsign leading the utterance: "<callsign>, <content>" (STT renders
    # the number as spoken digits by this point — the brain folds figures).
    if not re.match(rf"^\s*{re.escape(low_cs)}\b", utterance) \
            and not re.match(rf"^\s*\w+,\s*{re.escape(low_cs)}\b", utterance):
        return ""
    # The content after the callsign must LOOK like a readback (values/verbs,
    # not a bare check-in), otherwise a plain repetition of their own callsign
    # would be taught the wrong way.
    content = re.sub(rf"^\s*(\w+,)?\s*{re.escape(low_cs)}\b", "", utterance)
    if not re.search(
            r"\b(cleared|line up|hold short|holding short|runway|angels|"
            r"descend|descent|climb|exit|in use|qnh|readback|report|"
            r"turning|turn|heading)\b", content):
        return ""
    return "a readback ends with your callsign — there is none before it"


def note(utterance: str) -> str:
    """The canonical SOP call a sloppy utterance deviates from, or "".

    Returns just the canonical wording ("requesting taxi") — the brain wraps
    it in the `coach_wording` phraseology template (the "Apollo suggests:"
    voice). The brain only asks in coach mode; in casual mode the coach is
    never invoked at all. The brain already suppresses notes on semantic
    feedback ("readback incomplete"): one corrective point per transmission.
    """
    if not utterance:
        return ""
    for rule in _RULES:
        if rule.sloppy.search(utterance) and not (
                rule.unless and rule.unless.search(utterance)):
            return rule.proper
    return ""
