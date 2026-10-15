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
# common English word). Applied as whole-word substitutions. Keys are the
# lower-cased base name; multi-word values are matched as phrases (spaces are
# made flexible in the callsign regex), so "spring fail" also catches
# "Springfail".
#
# The list covers every client-slot flight in MA_Training_CA plus the original
# defaults. Matching and STT biasing should *select* the rows for the callsign
# DCS currently reports, not score the whole dictionary on every transmission.
KNOWN_ERRORS: dict[str, list[str]] = {
    # Standard DCS NATO flight callsigns. Live STT (Ford→forward, Dodge→touch)
    # is kept even when it is not a phonetic neighbour.
    "anvil": ["amble", "annville", "an ville"],
    "boar": ["bore", "boor", "board"],
    "chevy": ["shevy", "chevrolet", "chevy's", "chevvy", "chevrie"],
    "colt": ["coat", "cult", "bolt", "cold", "colt's"],
    "dodge": ["dodger", "dodges", "touch", "touched", "todger", "dodge's"],
    "enfield": ["en field", "anfield", "enfeld", "infield", "enfiled"],
    "ford": ["fort", "four", "fourth", "fork", "forward", "forwards",
             "ford's", "foard"],
    "hawg": ["hog", "hawk", "hulk", "hawgs"],
    "hornet": ["hornets", "hornett", "horn it"],
    "pontiac": ["pontyack", "pontiack", "ponty ac", "pond i ac"],
    "springfield": ["spring fail", "spring failed", "spring fails",
                    "spring field", "springpill", "springville", "spring pearl"],
    "uzi": ["oozie", "uzzie", "oozy", "uzy", "uzi's"],
    "viper": ["vyper", "vipper", "piper", "viper's"],

    # A-10 / attack
    "pig": ["pick", "big", "pigg", "piggs", "peg"],
    "tusk": ["dusk", "task", "tusked", "tusks"],

    # F/A-18
    "check": ["cheque", "chick", "checks"],
    "devil": ["devel", "devils", "double"],
    "hawk": ["hawg", "hock", "hulk", "hawks", "hog"],
    "joker": ["choker", "jokers", "jocker"],
    "jury": ["jewry", "jerry"],
    "ragin": ["raging", "rage in", "raygen", "ragan"],
    "ram": ["rom", "ramm", "lamb"],
    "roman": ["rowman", "roaming", "roam in"],
    "snake": ["snakes", "snek", "shnake"],
    "squid": ["squish", "skwid"],
    "sting": ["stink", "steeng"],

    # F-15E
    "best": ["beast", "bests"],
    "dude": ["dood", "doomed", "dudes"],
    "gunny": ["gummy", "gunnie"],
    "jazz": ["chas", "chaz", "jass"],
    "rage": ["rays"],
    "sled": ["slid", "sledge", "sleds"],
    "sniper": ["snipers", "sniper's"],
    "tahoe": ["taho", "tahoes", "toyota"],
    "thud": ["third", "stud"],
    "trek": ["track", "treck", "trekk"],

    # Heavy / bomber
    "bone": ["bohn", "bones", "bown"],
    "buff": ["buffs", "bough", "tough"],
    "dump": ["dumb", "dumps"],
    "kenworth": ["can worth", "ken worth"],
    "vader": ["vador", "evader", "veider", "invader", "vader's", "beta"],

    # AWACS
    "darkstar": ["dark star", "darcstar", "darkstar's", "dark start", "dockstar"],
    "focus": ["fokus", "phocus", "folks"],
    "magic": ["majic", "magik", "madgik"],
    "overlord": ["over lord", "overload", "overlord's"],
    "wizard": ["wizzard", "wisard", "lizard"],

    # Tanker
    "arco": ["arcko", "ourco", "our co", "ar co"],
    "shell": ["shel", "shall", "chel", "shells"],
    "texaco": ["taxaco", "texako", "texas go", "texas co", "texaco's", "taxico"],

    # JTAC
    "axeman": ["axe man", "x man", "axman"],
    "badger": ["batcher", "badgers"],
    "darknight": ["dark night", "darkknight", "dark knight"],
    "deathstar": ["death star", "dead star"],
    "eyeball": ["eye ball", "i ball", "eyeballs"],
    "ferret": ["ferritt", "fair it", "ferrette"],
    "firefly": ["fire fly", "fireflies"],
    "hammer": ["hamer", "hammers"],
    "jaguar": ["jagwire", "jag war"],
    "mantis": ["mantes", "manti", "mantiss"],
    "moonbeam": ["moon beam"],
    "pinpoint": ["pin point", "pen point"],
    "playboy": ["play boy", "playboys"],
    "pointer": ["point er", "pointers"],
    "warrior": ["warrier", "worrier"],
    "whiplash": ["whip lash"],

    # AH-64
    "apache": ["apachie", "a patch", "apatchy"],
    "bootleg": ["boot leg", "bootlegs"],
    "carnivor": ["carnivore", "carnivour"],
    "crow": ["krow", "crows", "crowe"],
    "gatling": ["gatlin"],
    "gunslinger": ["gun slinger", "guns slinger"],
    "hammerhead": ["hammer head", "hammerheads"],
    "palehorse": ["pale horse", "paled horse", "pail horse", "pale hors"],
    "saber": ["sabre", "savour"],
    "sioux": ["sue", "soo", "su"],

    # MA_Training_CA client slots not covered above.
    "adder": ["add her", "ada", "atter"],
    "beaver": ["beever", "bever", "believer"],
    "beluga": ["belouga", "balluga", "blue ga"],
    "camel": ["cammel", "carmel", "camera"],
    "cheetah": ["cheeta", "cheater"],
    "echo": ["eko", "eco", "ecko"],
    "empress": ["impress", "em pres", "empress's"],
    "epic": ["epik", "epoch"],
    "filthy": ["filter", "filthy's"],
    "frodo": ["froyo", "frodo's", "photo"],
    "gandalf": ["gandolf", "gun dalf", "gandalf's"],
    "gypsy": ["gipsy", "gypsie", "tipsy"],
    "heavy": ["hevy", "heavi", "heaviest"],
    "husky": ["huskie", "huskey"],
    "ironside": ["iron side", "iron sides"],
    "monster": ["monsters", "munster"],
    "mossie": ["mossy", "mozzie"],
    "nemo": ["nimo", "nemo's"],
    "nomad": ["nomads", "no mad", "nomad's"],
    "outlaw": ["out law", "outlaws"],
    "pinball": ["pin ball", "pinballs"],
    "python": ["pythons", "python's", "pie thon"],
    "quarterback": ["quarter back", "quarterbacks"],
    "rattler": ["rattlers", "ratler", "rattle her"],
    "savage": ["savages", "salvage"],
    "scary": ["scarey", "carry", "scari"],
    "scorpion": ["scorpions", "scorpian"],
    "shark": ["sharks", "sharp"],
    "turtle": ["turtles", "hurdle"],
    "venom": ["venum", "venoms"],
    "vodka": ["vodkas", "vodca"],
    "wedge": ["wedges", "wedg"],
    "whitehorse": ["white horse", "white hors"],
    "xray": ["x-ray", "x ray", "exray", "x rays"],
    "yankee": ["yankees", "yanky"],
    "yeti": ["yetti", "yetty"],
    "yvette": ["evette", "y vet"],
    "zeitgest": ["zeitgeist", "zeit geist", "sight gist"],
    "zeppelin": ["zepplin", "zeplin", "zeppelin's"],
    "zippo": ["zipo", "zippos"],
    "zircon": ["zircon's", "circle on"],
    "zodiac": ["zodiak", "zodiacs"],
    "zombie": ["zombies", "zombi"],
    "zooty": ["snooty", "zootie", "duty"],
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


# Spoken forms of the flight numbers pilots actually say ("Chevy one one").
_SPOKEN_NUMBERS = ["one", "two", "three", "four"]


def _callsign_hints(names: list[str], *, variants_per_name: int = 3) -> list[str]:
    """Expand flight names into the spellings Whisper is likely to hear.

    For each name: the canonical spelling, its top phonetic variants (the whole
    point of `generate_variants` — STT mishears them), and the spoken-number
    forms ("Chevy one", "Chevy one one") since pilots say the number as words.
    Ordered so the canonical names come first (they matter most).
    """
    canonical: list[str] = []
    expanded: list[str] = []
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        canonical.append(name)
        low = name.lower()
        # Top phonetic variants (skip the name itself).
        for variant in generate_variants(name)[:variants_per_name]:
            if variant.lower() != low:
                expanded.append(variant)
        # Lexical STT substitutions for *this* flight only (e.g. "forward" for
        # Ford). These are the words Whisper actually emits, so they belong in
        # the hotword list — generate_variants never produces them.
        for err in KNOWN_ERRORS.get(low, []):
            if err.lower() != low:
                expanded.append(err)
        # Spoken-number forms: "Chevy one", "Chevy one one".
        for num in _SPOKEN_NUMBERS:
            expanded.append(f"{name} {num}")
        expanded.append(f"{name} {_SPOKEN_NUMBERS[0]} {_SPOKEN_NUMBERS[0]}")
    return canonical + expanded


def stt_hint(callsigns: list[str] | None = None,
             airfield_names: list[str] | None = None,
             *, max_chars: int = 900) -> str:
    """Build a bias string for STT (`faster-whisper` `hotwords`/`initial_prompt`).

    The **mission callsigns come first** because a misheard callsign is the one
    error the bot cannot recover from: without it the transmission is treated as
    \"not for me\" and ignored. Each callsign is expanded into its likely
    mishearings and spoken-number forms (see `_callsign_hints`). Agencies and
    generic operations follow. The result is de-duplicated (case-insensitively)
    and length-capped, since Whisper's prompt context is limited (extra tokens
    are truncated anyway, and a huge prompt can slow decoding).
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(words) -> None:
        for word in words or []:
            word = (word or "").strip()
            if word and word.lower() not in seen:
                seen.add(word.lower())
                parts.append(word)

    add(_callsign_hints(callsigns or []))  # flight names + variants + numbers
    add(airfield_names)                    # e.g. "Gudauta", "Kutaisi"
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
