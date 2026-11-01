"""Explicit, session-scoped DEBUG markers for post-flight investigation."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path


_DEBUG_PREFIX = re.compile(r"^\s*DEBUG\b(.*)$", re.IGNORECASE | re.DOTALL)
_PILOT_STATE_FIELDS = (
    "phase", "last_controller", "checked_in_at", "pending_handoff_to",
    "requested_handoff_to", "entry_gate", "exit_gate", "vector_heading",
    "vector_runway", "cleared_inbound", "cleared_landing", "break_cleared",
    "pattern_flown", "go_around_issued", "go_around_circuit", "descend_issued",
    "descend_intermediate", "climb_issued", "awaiting_takeoff",
    "awaiting_landing", "departure_handoff_announced",
    "landing_handoff_announced", "rtb_requested", "formation",
    "readback_fails", "last_clearance", "last_reply",
)


def parse_debug_mark(text: str) -> str | None:
    """Return the note for a DEBUG marker, or None for ordinary radio text."""
    match = _DEBUG_PREFIX.match(text or "")
    if not match:
        return None
    note = match.group(1).strip().lstrip(":,-; ").strip()
    # Keep a hand-entered note useful in chat and bounded in saved records.
    note = " ".join(note.split())
    return note[:1000]


def pilot_state_snapshot(brain) -> dict[str, dict[str, object]]:
    """Copy the useful per-pilot ATC state without serializing live objects."""
    result = {}
    for callsign, pilot in sorted(brain.pilots.items()):
        state = {}
        for name in _PILOT_STATE_FIELDS:
            value = getattr(pilot, name, None)
            if hasattr(value, "value"):
                value = value.value
            state[name] = value
        result[callsign] = state
    return result


def aircraft_snapshot(units) -> list[dict[str, object]]:
    """Return JSON-ready aircraft fields from the bridge's unit snapshot."""
    result = []
    for unit in units:
        if is_dataclass(unit):
            result.append(asdict(unit))
        elif isinstance(unit, dict):
            result.append(dict(unit))
    return result


class DebugMarkWriter:
    """Append JSONL markers to one persistent file per bot session."""

    def __init__(self, directory: Path | str, airfield: str,
                 version: str, git_sha: str,
                 started_at: dt.datetime | None = None):
        self.directory = Path(directory)
        self.airfield = airfield
        self.version = version or "dev"
        self.git_sha = git_sha or "unknown"
        started_at = started_at or dt.datetime.now(dt.timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        started_at = started_at.astimezone(dt.timezone.utc)
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-", airfield.lower()).strip("-") or "field"
        self.session_id = f"{slug}-{stamp}-{self.git_sha[:12]}"
        self.path = self.directory / f"debug_marks_{slug}_{stamp}.jsonl"
        self._sequence = 0
        self._lock = threading.Lock()

    def append(self, event: dict[str, object]) -> int:
        """Append one complete record and flush it for durable inspection."""
        with self._lock:
            self._sequence += 1
            record = {
                "schema": 1,
                "event": "debug_mark",
                "session_id": self.session_id,
                "sequence": self._sequence,
                "recorded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z"),
                "airfield": self.airfield,
                "atc_version": self.version,
                "atc_git_sha": self.git_sha,
                **event,
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
            return self._sequence
