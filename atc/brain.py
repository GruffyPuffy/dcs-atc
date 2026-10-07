"""Rules-based ATC brain: recognize intents, produce canned ATC replies.

Deliberately dumb and deterministic — no LLM. Regexes tolerate the STT errors
we see in practice ("cold one" for "Colt 1", "Kutais/Kutasi" for "Kutaisi").
"""

import re

TOWER = "Kutaisi Tower"
RUNWAY = "25"

# callsign words the STT produces for "Colt 1"
CALLSIGN_RE = re.compile(
    r"\b(colt|cold|coat|cult|bolt|colt(?:\s|-)?one|cold(?:\s|-)?one)\s*(one|1|won)?\b",
    re.IGNORECASE)


def extract_callsign(text: str) -> str | None:
    match = CALLSIGN_RE.search(text)
    if not match:
        return None
    return "Colt 1"


class AtcBrain:
    """State machine per aircraft. Recognizes intents, returns reply text."""

    def __init__(self, tower: str = TOWER, runway: str = RUNWAY):
        self.tower = tower
        self.runway = runway
        self.states: dict[str, str] = {}  # callsign -> phase

    def handle(self, text: str) -> str | None:
        """Return the ATC reply for a pilot transmission, or None if we
        did not understand it (no callsign, unknown request)."""
        callsign = extract_callsign(text)
        if not callsign:
            return None
        low = text.lower()
        state = self.states.get(callsign, "")

        if re.search(r"\b(request|asking for)\b.*\btaxi\b|\btaxi\b.*\b(startup|start up|start)\b", low):
            self.states[callsign] = "taxi"
            return (f"{callsign}, {self.tower}, taxi to runway {self.runway} "
                    f"via alpha, hold short of runway {self.runway}.")
        if re.search(r"\bhold short\b", low) and state == "taxi":
            return f"{callsign}, {self.tower}, hold short runway {self.runway}."
        if re.search(r"\bready for departure\b|\bready for takeoff\b|\bready\b", low) \
                and state in ("taxi", "holding"):
            self.states[callsign] = "departure"
            return (f"{callsign}, {self.tower}, wind calm, runway {self.runway}, "
                    f"cleared for takeoff.")
        if re.search(r"\binbound\b|\bfinal\b|\bon approach\b|\blanding\b", low):
            self.states[callsign] = "inbound"
            return (f"{callsign}, {self.tower}, runway {self.runway}, wind calm, "
                    f"cleared to land.")
        if re.search(r"\bchecking (in|out)\b|\bwith you\b|\babort(s|ing)?\b", low):
            return f"{callsign}, {self.tower}, roger."
        if re.search(r"\b(reading back|roger|wilco|copy)\b", low):
            return f"{callsign}, {self.tower}, roger."
        # bare callsign / unintelligible request: ask them to say again
        return f"{callsign}, {self.tower}, say again."
