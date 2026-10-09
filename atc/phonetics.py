"""Generate STT-tolerant variants of callsigns from phonetic confusion rules.

Radio audio is noisy and Whisper mishears consonants and vowels in predictable
ways: voiced/unvoiced pairs swap (t<->d, p<->b, k<->g), nasals blur (m<->n),
and vowels drift (o<->u, a<->e). Rather than hand-listing every variant per
callsign, we derive them from these rules.

`generate_variants("Colt")` yields cold, bolt, cult, kolt, cort, coat, ... —
the variants we previously maintained by hand, plus the ones we had not thought
of. Variants are ranked by edit distance so the most likely come first.
"""

from __future__ import annotations

# Letters that are easily confused over a noisy radio or by STT. Each maps to
# the letters it can be misheard as. Kept deliberately conservative: only
# genuine phonetic neighbours, so we do not generate wild false positives.
CONFUSIONS: dict[str, str] = {
    # voiced / unvoiced plosive pairs
    "t": "d", "d": "t",
    "p": "b", "b": "p",
    "k": "gc", "g": "k", "c": "kb",
    # fricatives
    "f": "v", "v": "fb",
    "s": "z", "z": "s",
    # nasals
    "m": "n", "n": "m",
    # liquids
    "l": "r", "r": "l",
    # glides / affricates
    "w": "v", "j": "g",
    # vowels drift
    "a": "eo", "e": "ai", "i": "ey", "o": "ua", "u": "o", "y": "i",
}

# Letters commonly swallowed or dropped by STT (especially in the middle).
DROPPABLE = set("lrtdh")

# Extra known STT errors that are not pure phonetics (e.g. Whisper guessing a
# different word). Applied as whole-word substitutions. Keys are the
# lower-cased base name; multi-word values are matched as phrases (spaces are
# made flexible in the callsign regex), so "spring fail" also catches
# "Springfail".
KNOWN_ERRORS: dict[str, list[str]] = {
    "colt": ["coat", "cult", "bolt", "cold", "colt's"],
    "ford": ["fort", "four", "fourth", "fork"],
    "hawg": ["hog", "hawk", "hulk"],
    "boar": ["bore", "boar"],
    "uzi": ["oozie", "uzzie", "oozy"],
    "viper": ["vyper", "vipper", "piper"],
    "dodge": ["dodger", "dodges"],
    # "Springfield" is long and Whisper reliably fractures it (observed live:
    # "Spring fail", "SpringPill", "springfield"). Cover the common fractures.
    "springfield": ["spring fail", "spring failed", "spring fails", "springfield",
                    "spring field", "springpill", "springville", "spring pearl"],
    "enfield": ["en field", "anfield", "enfeld"],
    "pontiac": ["pontiac", "pontyack", "pontiack"],
    "chevy": ["chevy", "shevy", "chevy"],
    "anvil": ["anvil", "amble", "annville"],
    "hornet": ["hornet", "hornets", "hornett"],
}

# Generic phraseology that recurs on every frequency. Kept short and after the
# callsigns in the hint: Whisper's context window is small, and callsigns (the
# one token that must be recognised *exactly*) get the strongest bias by being
# closest to the prompt.
OPERATIONS_HINT = [
    "ready to copy clearance", "cleared taxi", "hold short", "line up and wait",
    "cleared for takeoff", "cleared to land", "inbound", "on final",
    "runway in use", "QNH", "wind calm", "with information", "requesting taxi",
    "ready for departure", "airborne", "descend and maintain", "radar contact",
]

# Agency roles as heard over the radio (spelled as spoken, not the full name).
AGENCY_HINT = ["Tower", "Ground", "Control", "Approach", "Departure", "ATIS"]


def stt_hint(callsigns: list[str] | None = None,
             airfield_names: list[str] | None = None,
             *, max_chars: int = 900) -> str:
    """Build a bias string for STT (`faster-whisper` `hotwords`/`initial_prompt`).

    The **mission callsigns come first** because a misheard callsign is the one
    error the bot cannot recover from: without it the transmission is treated as
    \"not for me\" and ignored. Agencies and generic operations follow. The
    result is de-duplicated (case-insensitively) and length-capped, since
    Whisper's prompt context is limited (extra tokens are truncated anyway, and
    a huge prompt can slow decoding).
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(words) -> None:
        for word in words or []:
            word = (word or "").strip()
            if word and word.lower() not in seen:
                seen.add(word.lower())
                parts.append(word)

    add(callsigns)         # flight names — highest-value bias
    add(airfield_names)    # e.g. "Gudauta", "Kutaisi"
    add(AGENCY_HINT)
    add(OPERATIONS_HINT)
    return ", ".join(parts)[:max_chars]


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


def generate_variants(name: str, *, max_edits: int = 1, min_length: int = 3,
                      limit: int = 48) -> list[str]:
    """Return plausible STT mishearings of `name`, most likely first.

    - single substitutions from CONFUSIONS
    - single deletions of DROPPABLE letters
    - known whole-word errors
    - (if max_edits >= 2) combinations of the above, for short names
    """
    name = name.lower().strip()
    if not name:
        return []

    variants: set[str] = {name}

    # single substitutions
    for i, ch in enumerate(name):
        for repl in CONFUSIONS.get(ch, ""):
            variants.add(name[:i] + repl + name[i + 1:])

    # single deletions of droppable letters
    for i, ch in enumerate(name):
        if ch in DROPPABLE and len(name) - 1 >= min_length:
            variants.add(name[:i] + name[i + 1:])

    # known whole-word errors
    variants.update(KNOWN_ERRORS.get(name, []))

    # second-order edits (substitution + deletion) for short names, where a
    # single edit is not enough to cover the common mishearings
    if max_edits >= 2 and len(name) <= 6:
        base = list(variants)
        for variant in base:
            for i, ch in enumerate(variant):
                if ch in DROPPABLE and len(variant) - 1 >= min_length:
                    variants.add(variant[:i] + variant[i + 1:])

    # rank: closest to the original first, then alphabetical
    ranked = sorted(variants, key=lambda v: (_edit_distance(name, v), v))
    ranked = [v for v in ranked if len(v) >= min_length]
    return ranked[:limit]


def variants_for(names: list[str], **kwargs) -> dict[str, list[str]]:
    """Generate variants for several callsigns at once."""
    return {name: generate_variants(name, **kwargs) for name in names}
