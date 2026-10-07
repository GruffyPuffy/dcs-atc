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
# different word). Applied as whole-word substitutions.
KNOWN_ERRORS: dict[str, list[str]] = {
    "colt": ["coat", "cult", "bolt", "cold"],
    "ford": ["fort", "four"],
    "hawg": ["hog", "hawk"],
    "boar": ["bore", "boar"],
    "uzi": ["oozie", "uzzie"],
    "viper": ["vyper", "vipper"],
}


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
