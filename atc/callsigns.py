"""Callsign recognition driven by the mission's player slots.

DCS exposes every player slot's callsign in `env.mission` (see the bridge's
`callsigns` op). We use those names so the bot only reacts to callsigns that
actually exist in the loaded mission, instead of guessing.

STT mangles callsigns, so matching is tolerant:
- flight-name variants are GENERATED from phonetic confusion rules
  (`phonetics.generate_variants`), e.g. Colt -> cold/bolt/coat/cult/kolt/...
- flight numbers accept digits or spoken words, including homophones
  (one/won, two/to/too, three/tree, four/for, eight/ate)

A callsign is normalised to "Name N" (e.g. "Colt 1") or "Name N-M" for a
flight+element ("Colt 1-1").
"""

from __future__ import annotations

import re

from phonetics import generate_variants

NUMBER_WORDS = {
    "one": "1", "won": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3",
    "four": "4", "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8", "ate": "8",
    "nine": "9",
}

# Fallback names if the mission callsigns are unavailable (e.g. no state bridge).
DEFAULT_NAMES = ["Colt", "Springfield", "Enfield", "Chevy", "Pontiac", "Uzi",
                 "Dodge", "Ford", "Viper", "Hawg", "Hornet", "Boar", "Anvil"]


def _number_pattern() -> str:
    words = "|".join(NUMBER_WORDS)
    return rf"(?:[1-9]|{words})"


def _normalise_number(token: str) -> str:
    token = token.lower()
    return NUMBER_WORDS.get(token, token)


class CallsignRegistry:
    """Recognises callsigns from a known set of flight names."""

    def __init__(self, names: list[str] | None = None):
        names = names or DEFAULT_NAMES
        # unique, non-empty, sorted longest-first so "springfield" wins over "s"
        self.names = sorted({n.strip() for n in names if n and n.strip()},
                            key=len, reverse=True)
        # flight name (lowercase) -> generated STT variants. Drop variants that
        # are themselves number words (e.g. "four" for "Ford"), which would
        # otherwise match phrases like "four two" as a callsign.
        self.variants: dict[str, list[str]] = {
            name.lower(): [v for v in generate_variants(name)
                           if v not in NUMBER_WORDS]
            for name in self.names
        }
        self._regex = self._build_regex()

    def _build_regex(self) -> re.Pattern | None:
        if not self.names:
            return None
        alternatives = []
        for name in self.names:
            for variant in self.variants.get(name.lower(), [name.lower()]):
                # allow an optional space inside multi-word names
                alternatives.append(re.escape(variant).replace(r"\ ", r"\s*"))
        name_alt = "|".join(alternatives)
        num = _number_pattern()
        # "Name 1", "Name 1-1", "Name 11" (flight+element), with optional separators
        pattern = (
            rf"\b(?P<name>{name_alt})\s*[- ]?\s*"
            rf"(?P<num>{num})(?:\s*[- ]?\s*(?P<elem>{num}))?\b"
        )
        return re.compile(pattern, re.IGNORECASE)

    def extract(self, text: str) -> str | None:
        """Return a normalised callsign like 'Colt 1' or 'Colt 1-1', or None."""
        if self._regex is None:
            return None
        match = self._regex.search(text)
        if not match:
            return None
        name = match.group("name").strip().title()
        # canonicalise the name to the mission spelling if we can
        canonical = self._canonical_name(name)
        number = _normalise_number(match.group("num"))
        element = match.group("elem")
        if element:
            return f"{canonical} {number}-{_normalise_number(element)}"
        return f"{canonical} {number}"

    def _canonical_name(self, name: str) -> str:
        low = name.lower()
        for known in self.names:
            if known.lower() == low:
                return known
            if low in self.variants.get(known.lower(), []):
                return known
        return name

    @classmethod
    def from_mission(cls, slots: list[dict]) -> "CallsignRegistry":
        """Build from the bridge's callsign list.

        Each slot's `name` is the full callsign (e.g. "Springfield21"); the
        flight name is that with trailing digits stripped ("Springfield").
        """
        names = []
        for slot in slots:
            raw = (slot.get("name") or "").strip()
            if not raw:
                continue
            flight = re.sub(r"\d+$", "", raw).strip()
            if flight:
                names.append(flight)
        return cls(names or DEFAULT_NAMES)
