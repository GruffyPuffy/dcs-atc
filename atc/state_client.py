"""Client for the DCS state bridge (JSON socket, port 10309).

The bridge is the Saved Games hook `bridge/dcs_state_hook.lua`. This module is
the bot-side reader: it fetches the live mission picture (groups, airbases,
positions) and exposes helpers to find player aircraft.

The protocol is one JSON request per connection, one JSON reply line back.
"""

from __future__ import annotations

import json
import re
import socket
import uuid
from dataclasses import dataclass


def norm_identity(name: str) -> str:
    """Normalise a DCS/SRS name for identity matching.

    Lower-case and strip separators (so "Caveman_1" == "caveman-1"), but keep
    digits **significant**: DCS appends a number to make duplicate player names
    unique ("Caveman" and "Caveman-1" can be two *different* people), so
    dropping it would wrongly merge them.
    """
    return re.sub(r"[\s_-]+", "", (name or "").lower())


def slot_callsign(unit_name: str) -> str:
    """Derive a flight callsign from a DCS unit name.

    DCS unit names can be a flight name with an element attached, e.g.
    "Springfield11" -> "Springfield 1-1" (flight 1, element 1). Returns "" if
    the name has no trailing digits to split.
    """
    m = re.match(r"^([A-Za-z]+)(\d+)$", unit_name or "")
    if not m:
        return ""
    name, digits = m.group(1), m.group(2)
    if len(digits) >= 2:
        return f"{name} {digits[0]}-{digits[1]}"
    return f"{name} {digits}"


def resolve_player_unit(units, srs_name: str, learned_callsign: str = "",
                        solo: bool = False, speaker_pos=None):
    """Find the live unit for a transmitting pilot, None if not resolvable.

    The SRS name and the DCS player name **need not be the same** (players can
    set one name in DCS and a different one in SRS), so matching on the player
    name alone is not enough. This resolves in confidence order and **never
    guesses when it could be wrong**:

    1. `player` equals the SRS name (the common case — SRS reports the DCS
       player name, and most players use the same name in DCS and SRS).
    2. `player` equals a callsign the brain already learned for this speaker.
    3. `player` equals the flight callsign the *unit's own name* implies
       (e.g. SRS "Springfield11" <-> unit "Springfield11" -> flight "Springfield 1-1").
    4. `speaker_pos` (the SRS-reported client position) matches exactly one live
       player unit — an independent signal that disambiguates duplicate names.
    5. Solo training: exactly one live player unit — take it.

    Returns None when ambiguous (more than one candidate at a winning step) so
    the caller leaves the pilot unresolved rather than binding to the wrong
    aircraft. `learned_callsign` is the flight callsign the brain has mapped the
    speaker to; the caller owns that mapping.
    """
    units = list(units or [])
    if not units:
        return None

    def by_player(candidate: str):
        key = norm_identity(candidate)
        if not key:
            return []
        exact = [u for u in units if (u.player or "").lower() == candidate.lower()]
        return exact or [u for u in units if norm_identity(u.player) == key]

    # 1) SRS name == DCS player name.
    hits = by_player(srs_name)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return None  # ambiguous — do not guess

    # 2) The player name matches a callsign we already learned for this speaker.
    if learned_callsign:
        hits = by_player(learned_callsign)
        if len(hits) == 1:
            return hits[0]

    # 3) The SRS name is a *slot* callsign, and a unit's own name implies the
    #    same flight callsign (handles DCS name != SRS name, with a shared slot).
    want = slot_callsign(srs_name)
    if want:
        hits = [u for u in units
                if slot_callsign(u.callsign) == want
                or norm_identity(u.player) == norm_identity(want)]
        if len(hits) == 1:
            return hits[0]

    # 4) The SRS client's own reported position matches exactly one unit.
    if speaker_pos is not None:
        near = [u for u in units
                if _within_nm(speaker_pos, (u.lat, u.lon), 3.0)]
        if len(near) == 1:
            return near[0]

    # 5) Solo: only one player online, so it must be them.
    if solo and len(units) == 1:
        return units[0]
    return None


def _within_nm(a, b, max_nm: float) -> bool:
    """True if two lat/lon points are within `max_nm` (flat-earth, small)."""
    import math
    dlat = (b[0] - a[0]) * 60.0
    dlon = (b[1] - a[1]) * 60.0 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlon) <= max_nm


@dataclass
class Aircraft:
    callsign: str
    player: str
    type: str
    lat: float
    lon: float
    alt_ft: float
    heading: float
    coalition: int
    category: int = 0  # DCS group category: 0 plane, 1 helicopter, 2 ground, 3 ship
    speed_kt: float = 0.0  # ground speed (from the velocity vector)


class StateClient:
    """Reads live state from the DCS bridge."""

    def __init__(self, host: str = "127.0.0.1", port: int = 10309,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def exchange(self, operation: str, **fields: object) -> dict:
        request = {"v": 1, "id": uuid.uuid4().hex, "op": operation, **fields}
        payload = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
            conn.settimeout(self.timeout)
            conn.sendall(payload)
            with conn.makefile("rb") as stream:
                line = stream.readline(2 * 1024 * 1024 + 1)
        if not line.endswith(b"\n"):
            raise RuntimeError("No complete response from DCS bridge")
        reply = json.loads(line)
        if not isinstance(reply, dict) or reply.get("v") != 1 or reply.get("id") != request["id"]:
            raise RuntimeError("Unexpected response from DCS bridge")
        return reply

    def status(self) -> dict:
        """Raw status result: {'groups': [...], 'airbases': [...]}."""
        reply = self.exchange("status")
        if not reply.get("ok"):
            raise RuntimeError(f"state bridge error: {reply.get('error')}")
        return reply["result"]

    def _units(self, players_only: bool) -> list[Aircraft]:
        result = self.status()
        out: list[Aircraft] = []
        for group in result.get("groups", []):
            for unit in group.get("units", []):
                if players_only and not unit.get("player"):
                    continue
                out.append(Aircraft(
                    callsign=unit.get("name", ""),
                    player=unit.get("player", ""),
                    type=unit.get("type", ""),
                    lat=float(unit["lat"]),
                    lon=float(unit["lon"]),
                    alt_ft=float(unit["alt"]) * 3.280839895,
                    heading=float(unit.get("heading", 0.0)),
                    coalition=int(group.get("coalition", 0)),
                    category=int(group.get("category", 0)),
                    speed_kt=float(unit.get("speed", 0.0)) * 1.9438444924,
                ))
        return out

    def aircraft(self) -> list[Aircraft]:
        """All player-occupied aircraft currently in the mission."""
        return self._units(players_only=True)

    def all_units(self) -> list[Aircraft]:
        """Every unit (players and AI) — used for runway occupancy checks."""
        return self._units(players_only=False)

    def airbase(self, name: str) -> dict | None:
        """Airbase entry by (case-insensitive) name."""
        for base in self.status().get("airbases", []):
            if base.get("name", "").lower() == name.lower():
                return base
        return None

    def callsigns(self) -> list[dict]:
        """Player-slot callsigns defined in the mission (env.mission)."""
        reply = self.exchange("callsigns")
        if not reply.get("ok"):
            raise RuntimeError(f"state bridge error: {reply.get('error')}")
        return reply["result"].get("callsigns", [])

    def weather(self) -> dict:
        """Live mission weather (wind, QNH, clouds, visibility)."""
        reply = self.exchange("weather")
        if not reply.get("ok"):
            raise RuntimeError(f"state bridge error: {reply.get('error')}")
        return reply["result"]

    def tower(self, name: str) -> dict | None:
        """Tower/dispatcher position for an airbase, or None if unavailable."""
        reply = self.exchange("tower", airbase=name)
        if not reply.get("ok"):
            return None
        return reply["result"]

    def message(self, text: str, player: str = "",
                duration: float = 8.0) -> bool:
        """Post text to the in-game chat/message log. To `player` by name when
        given (else the whole mission). Used by --debug to mirror the ATC radio
        exchange back to the pilot. Returns True if the bridge accepted it."""
        reply = self.exchange("message", text=text, player=player,
                              duration=duration)
        return bool(reply.get("ok"))
