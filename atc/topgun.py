"""TopGun easter egg: "request to bust the tower" -> the tower's reply.

This is a *hidden* gag, kept entirely in code (never in phraseology.json and
never in the docs). It is deliberately self-contained so it **cannot touch the
ATC state machine**: a pilot who asks to bust the tower gets a canned reply and
is flagged; if they then actually fly the stunt (close to the tower, low and
fast) the tower plays a one-shot "coffee spill" sting.

Both audio stings are optional drop-in WAVs anywhere on `sys.path` / cwd /
`atc/audio/`:

    topgun_negative.wav   # the "Negative, Ghostrider, the pattern is full." line
    topgun_bust.wav       # the tower's reaction when the tower is actually busted

Nothing copyrighted is ever committed. The easter egg is live when **either**:

  * `topgun_negative.wav` is present (parse the real clip), or
  * a marker file `topgun.txt` is present (TTS-only — no audio).

so a user can `touch atc/topgun.txt` to switch it on with the normal TTS voice,
and drop the clip in for the real line. Both markers are gitignored, so a fresh
checkout is silent and the gag is off until a user opts in.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Film quotes. Kept here, not in phraseology.json (which is the documented,
# community-editable Master Arms SOP wording) and not in any doc.
NEGATIVE_LINE = "Negative, Ghost Rider, the pattern is full."
BUST_LINE = "Ghost Rider, you just busted my tower. The pattern is full. Knock it off."

# A request to fly at/through the tower: "request to bust the tower", "buzz the
# tower", "tower flyby", "beat up the field", "low pass the tower", "beat up the
# pattern", or the film's own "requesting a flyby" (no field named).
_REQUEST = re.compile(
    r"\b(?:"
    # a request verb + a stunt (the tower is optional, as in "request flyby")
    r"(?:request(?:ing)?(?:\s+(?:to|a))?|like\s+to|want\s+to|ask(?:ing)?\s+to|"
    r"cleared\s+to|may\s+i|can\s+i)\s+"
    r"(?:bust|buzz|beat\s+up|fly\s?by|flyby|low\s?pass|low\s+approach|"
    r"wave\s+the\s+wings|bomb|rock)\b"
    r"|"
    # a bare stunt aimed at the tower
    r"(?:bust|buzz|beat\s+up|fly\s?by|flyby|low\s?pass|low\s+approach|"
    r"bomb|rock)\b"
    r".*?\btower\b"
    r"|\btower\b.*?\b(?:flyby|fly\s?by|buzz|bust)\b"
    r"|\bbeat\s+up\s+the\s+(?:field|pattern)\b"
    r")",
    re.IGNORECASE,
)

# Fixed built-in stalls of the real line (so the flag lifts early), plus the
# boxed anvil: usable anywhere.
_STALLS = re.compile(
    r"\b(?:too close for missiles|switching to guns|because of the plaque|"
    r"pissing me off|bogey dope|talk to me goose)\b",
    re.IGNORECASE,
)

# How close (nm), how low (ft AGL) and how fast (kt) counts as a bust, and how
# long the state must have held before the sting fires (s).
BUST_DISTANCE_NM = 1.5
BUST_AGL_FT = 300.0
BUST_SPEED_KT = 250.0
BUST_HOLD_S = 1.5


def negative_clip() -> Path | None:
    """The primary drop-in clip, or None (then the line is spoken by TTS)."""
    return clip("topgun_negative")


def _marker() -> Path | None:
    """The TTS-only enable marker (`topgun.txt`) if present, else None."""
    here = Path(__file__).resolve().parent
    for path in (here / "topgun.txt", Path.cwd() / "topgun.txt"):
        if path.is_file():
            return path
    return None


def mode() -> str:
    """Which easter-egg variant is active, by strict precedence:

        "wav"  the primary clip (`topgun_negative.wav`) is present -> play audio
        "tts"  no clip, but the `topgun.txt` marker exists        -> speak it
        "off"  neither                                            -> disabled
    """
    if negative_clip() is not None:
        return "wav"
    if _marker() is not None:
        return "tts"
    return "off"


def enabled() -> bool:
    """True unless the easter egg is off (see `mode`)."""
    return mode() != "off"


def spike(text: str) -> bool:
    """True if the pilot asked to bust/buzz/beat up the tower."""
    return bool(_REQUEST.search(text or ""))


def stall(text: str) -> bool:
    """True for a known TopGun one-liner the pill should lift on."""
    return bool(_STALLS.search(text or ""))


def _wav_dirs() -> list[Path]:
    """Folders searched for the optional drop-in clips, in order.

    An explicit `ATC_TOPGUN_AUDIO` directory wins (so a user can point at their
    own copy anywhere), then the conventional `audio/` folder, then the working
    directory.
    """
    here = Path(__file__).resolve().parent
    cwd = Path.cwd()
    candidates: list[Path] = []
    if env_dir := os.environ.get("ATC_TOPGUN_AUDIO"):
        candidates.append(Path(env_dir))
    candidates += [here / "audio", here.parent / "audio", cwd / "audio", cwd]
    seen: set[Path] = set()
    out: list[Path] = []
    for directory in candidates:
        resolved = directory.resolve()
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def clip(name: str) -> Path | None:
    """Path to a drop-in clip (`<name>.wav`) if one is present, else None."""
    for directory in _wav_dirs():
        path = directory / f"{name}.wav"
        if path.is_file():
            return path
    return None


class BustWatch:
    """Per-callsign timer for the tower-bust sting.

    Fires once the *whole* bust condition (close to the tower AND low AND fast)
    has held continuously for `hold_s`. The timer starts only when all three are
    true and resets the moment any of them breaks, so a wide/high entry that
    dives into the window still has to hold low+fast for the full duration.
    Each callsign fires at most once until `reset`.
    """

    def __init__(self, hold_s: float = BUST_HOLD_S,
                 distance_nm: float = BUST_DISTANCE_NM,
                 agl_ft: float = BUST_AGL_FT,
                 speed_kt: float = BUST_SPEED_KT):
        self.hold_s = hold_s
        self.distance_nm = distance_nm
        self.agl_ft = agl_ft
        self.speed_kt = speed_kt
        self._since: dict[str, float] = {}
        self._fired: set[str] = set()

    def update(self, callsign: str, near: bool, low: bool, fast: bool,
               now: float) -> bool:
        """Feed one sample; return True the first time the hold is satisfied."""
        if callsign in self._fired:
            return False
        if near and low and fast:
            started = self._since.setdefault(callsign, now)
            if now - started >= self.hold_s:
                self._fired.add(callsign)
                self._since.pop(callsign, None)
                return True
        else:
            self._since.pop(callsign, None)
        return False

    def reset(self, callsign: str) -> None:
        """Allow `callsign` to fire again (e.g. after re-arming)."""
        self._fired.discard(callsign)
        self._since.pop(callsign, None)
