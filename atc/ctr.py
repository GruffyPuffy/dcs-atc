"""CTR boundary tracker: turn per-aircraft positions into enter/exit events.

Keeps the previous inside/outside state per callsign so we can react to
transitions (e.g. warn an aircraft that enters controlled airspace without a
clearance) instead of spamming on every position update.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from airspace import Airfield


class CtrEvent(str, Enum):
    NO_CHANGE = "NO_CHANGE"
    ENTERED = "ENTERED_CTR"
    EXITED = "EXITED_CTR"


@dataclass
class AircraftTrack:
    callsign: str
    inside: bool
    lat: float
    lon: float
    alt_ft: float
    distance_nm: float
    relative: str


class CtrTracker:
    """Tracks CTR membership per callsign for one airfield."""

    def __init__(self, airfield: Airfield):
        self.airfield = airfield
        self._inside: dict[str, bool] = {}

    def update(self, callsign: str, lat: float, lon: float,
               alt_ft: float) -> tuple[CtrEvent, AircraftTrack]:
        """Update one aircraft and return (event, track)."""
        inside = self.airfield.ctr.contains(lat, lon, alt_ft)
        previous = self._inside.get(callsign)

        if previous is None:
            event = CtrEvent.NO_CHANGE
        elif not previous and inside:
            event = CtrEvent.ENTERED
        elif previous and not inside:
            event = CtrEvent.EXITED
        else:
            event = CtrEvent.NO_CHANGE

        self._inside[callsign] = inside
        return event, self.track(callsign, lat, lon, alt_ft)

    def track(self, callsign: str, lat: float, lon: float,
              alt_ft: float) -> AircraftTrack:
        """Build a track for a position WITHOUT changing tracker state."""
        return AircraftTrack(
            callsign=callsign,
            inside=self.airfield.ctr.contains(lat, lon, alt_ft),
            lat=lat,
            lon=lon,
            alt_ft=alt_ft,
            distance_nm=self.airfield.ctr.distance_nm(lat, lon),
            relative=self.airfield.ctr.relative_position(lat, lon),
        )

    def is_inside(self, callsign: str) -> bool:
        return self._inside.get(callsign, False)

    def forget(self, callsign: str) -> None:
        self._inside.pop(callsign, None)
