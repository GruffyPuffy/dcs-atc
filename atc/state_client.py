"""Client for the DCS state bridge (JSON socket, port 10309).

The bridge is the Saved Games hook `bridge/dcs_state_hook.lua`. This module is
the bot-side reader: it fetches the live mission picture (groups, airbases,
positions) and exposes helpers to find player aircraft.

The protocol is one JSON request per connection, one JSON reply line back.
"""

from __future__ import annotations

import json
import socket
import uuid
from dataclasses import dataclass


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
