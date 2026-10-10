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

from phonetics import KNOWN_ERRORS, generate_variants
from state_client import canonical_callsign

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


def _speaker_key(name: str) -> str:
    """Normalise an SRS/DCS speaker name for speaker->callsign lookup.

    Shared with `state_client.norm_identity` (single source of truth):
    lower-case, strip separators, drop a trailing digit.
    """
    from state_client import norm_identity
    return norm_identity(name)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (small strings; simple DP is fine)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _word_distance(a: str, b: str, cap: int) -> int:
    """Edit distance between two words, or `cap+1` if it clearly exceeds `cap`.

    Length difference is a lower bound on edit distance, so this cheaply skips
    obviously-different pairs before running the full DP.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    return _edit_distance(a, b)


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
        # Known multi-word STT fractures (e.g. "spring fail" for "Springfield").
        # Exposed separately so the matcher can collapse the spaces: they must
        # match as a single flight word, not a name followed by a stray token.
        self.fractures: dict[str, list[str]] = {
            name.lower(): [v for v in KNOWN_ERRORS.get(name.lower(), [])
                           if " " in v]
            for name in self.names
        }
        self._regex = self._build_regex()
        # Lower-cased set of every accepted spelling, for fuzzy recovery.
        self._accepted: set[str] = set()
        for name in self.names:
            self._accepted.add(name.lower())
            for variant in self.variants.get(name.lower(), ()):
                self._accepted.add(variant)

    def _name_alt(self) -> str:
        """Regex alternation of every flight-name spelling (variants + fractures).

        Multi-word spellings have their spaces made optional so a fracture like
        "spring fail" matches both "spring fail" and "springfail".
        """
        alternatives = []
        for name in self.names:
            low = name.lower()
            for variant in [low, *self.variants.get(low, []),
                            *self.fractures.get(low, [])]:
                alternatives.append(re.escape(variant).replace(r"\ ", r"\s*"))
        # Longest first so "spring field" is preferred over a bare "field".
        alternatives.sort(key=len, reverse=True)
        return "|".join(alternatives)

    def _build_regex(self) -> re.Pattern | None:
        if not self.names:
            return None
        name_alt = self._name_alt()
        num = _number_pattern()
        # "Name 1", "Name 1-1", "Name 11" (flight+element), with optional separators
        pattern = (
            rf"\b(?P<name>{name_alt})\s*[- ]?\s*"
            rf"(?P<num>{num})(?:\s*[- ]?\s*(?P<elem>{num}))?\b"
        )
        return re.compile(pattern, re.IGNORECASE)

    def extract(self, text: str) -> str | None:
        """Return a normalised callsign like 'Colt 1' or 'Colt 1-1', or None."""
        if self._regex is not None:
            match = self._regex.search(text)
            if match:
                name = match.group("name").strip().title()
                # canonicalise the name to the mission spelling if we can
                canonical = self._canonical_name(name)
                number = _normalise_number(match.group("num"))
                element = match.group("elem")
                return canonical_callsign(canonical, number,
                                          _normalise_number(element) if element else None)
        # Nothing matched exactly — recover a near-miss on the flight name (the
        # number is usually intelligible even when the name is not).
        return self._fuzzy_extract(text)

    def _fuzzy_extract(self, text: str) -> str | None:
        """Recover a callsign whose flight name was misheard beyond the rules.

        Scans token windows for a word within a small edit distance of a known
        flight name (augmenting the generative variants with real STT errors),
        requiring a nearby flight number to avoid false positives. Returns None
        unless the recovered name is a configurable margin closer than the next
        best flight name, so an ambiguous guess never fabricates a callsign.
        """
        num = _number_pattern()
        word_num = re.compile(rf"^(?:{num})$", re.IGNORECASE)
        tokens = re.findall(r"[A-Za-z']+|\d+", text)
        if not tokens:
            return None
        max_distance = 2 if any(len(n) >= 7 for n in self.names) else 1
        for i, token in enumerate(tokens):
            # A number right after the name ("cold 1") or an attached digit
            # ("cold1") is what distinguishes a callsign from ordinary words.
            number, element = None, None
            if i + 1 < len(tokens) and word_num.match(tokens[i + 1]):
                number = tokens[i + 1]
                if i + 2 < len(tokens) and word_num.match(tokens[i + 2]):
                    element = tokens[i + 2]
            else:
                attached = re.match(rf"([A-Za-z']+)({num})$", token, re.IGNORECASE)
                if not attached:
                    continue
                token, number = attached.group(1), attached.group(2)
            # A number word ("four") can never be the flight name — a callsign
            # spans a name *and* a number ("Ford 2"), so skip the number itself.
            if token.lower() in NUMBER_WORDS:
                continue
            best, best_d, second_d = None, 99, 99
            for known in self.names:
                variants = [known.lower(), *self.variants.get(known.lower(), []),
                            *KNOWN_ERRORS.get(known.lower(), [])]
                # Never let a number word (e.g. "four" for "Ford") stand in for
                # the flight name: "four two" is not a callsign.
                variants = [v for v in variants if v not in NUMBER_WORDS]
                d = min(_word_distance(token.lower(), v, max_distance)
                        for v in variants)
                if d < best_d:
                    best, second_d, best_d = known, best_d, d
                elif d < second_d:
                    second_d = d
            if best is not None and best_d <= max_distance \
                    and second_d - best_d >= 1:
                canonical = best
                if element:
                    return canonical_callsign(canonical,
                                              _normalise_number(number),
                                              _normalise_number(element))
                return f"{canonical} {_normalise_number(number)}"
        return None

    def extract_name(self, text: str) -> str | None:
        """Return just a flight name (no number), e.g. 'Colt', or None.

        Used for calls that may omit the flight number (e.g. "Colt help").
        """
        if not self.names:
            return None
        pattern = re.compile(rf"\b(?P<name>{self._name_alt()})\b", re.IGNORECASE)
        match = pattern.search(text)
        if not match:
            return None
        return self._canonical_name(match.group("name").strip().title())

    def _canonical_name(self, name: str) -> str:
        low = name.lower()
        for known in self.names:
            if known.lower() == low:
                return known
            if low in self.variants.get(known.lower(), []):
                return known
        return name

    def speaker_spellings(self, who: str) -> list[str]:
        """Normalised keys under which an SRS speaker name may be stored.

        A speaker name is stored once per spelling so that a player whose SRS
        name reproduces a callsign we recognise (spaces or not, digits or not)
        still resolves. Returns a single normalised key for the name, plus each
        matching flight name normalised the same way (e.g. both "springfield"
        and "springfield1" for the flight "Springfield"), so the brain can look
        the mapping up consistently in either form.
        """
        spellings = [_speaker_key(who)]
        low = re.sub(r"[\s_-]+", "", (who or "").lower())
        for known in self.names:
            k = re.sub(r"[\s_-]+", "", known.lower())
            if low == k or low == k + "1":
                spellings.append(k)
        return list(dict.fromkeys(spellings))

    def looks_like(self, text: str, callsign: str) -> bool:
        """True if `text` plausibly refers to `callsign`'s flight name.

        Used to attribute a transmission to a *known* speaker when no callsign
        could be extracted: the flight name is matched loosely (known fractures
        and variants, a small edit distance, or a shared prefix — so "spring
        fail" / "springpill" count for "Springfield"). Deliberately generous:
        the caller only uses this for a pilot we have already identified, so a
        false positive attributes a call to the right person anyway.
        """
        flight = re.split(r"\s+", (callsign or "").strip())[0].lower()
        if not flight:
            return False
        variants = [flight, *self.variants.get(flight, []),
                    *KNOWN_ERRORS.get(flight, [])]
        # A number word ("four" for "Ford") must not count as the flight name.
        variants = [v for v in variants if v not in NUMBER_WORDS]

        def norm(s: str) -> str:
            return re.sub(r"[^a-z]", "", s.lower())

        joined = norm(text)
        for variant in variants:
            v = norm(variant)
            if v and v in joined:
                return True
        tokens = re.findall(r"[A-Za-z']+", text)
        for token in tokens:
            nt = norm(token)
            if not nt:
                continue
            for variant in variants:
                nv = norm(variant)
                if not nv:
                    continue
                if _word_distance(nt, nv, 2) <= 2:
                    return True
                # Shared prefix ("spring" for "springfield").
                prefix = 0
                for a, b in zip(nt, nv):
                    if a != b:
                        break
                    prefix += 1
                if prefix >= max(4, len(nv) // 2):
                    return True
        return False

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
