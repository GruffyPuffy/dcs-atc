"""Live map view for the ATC trainer (stdlib HTTP server + Leaflet).

Serves a single-page map showing the airfield (CTR, gates, runways, taxi
routes, parking) and every player aircraft with its callsign, flight phase and
controller. Data comes from the same live sources the bot uses:

- positions: the DCS state bridge (`state_client.StateClient`)
- phases:    the shared `AtcBrain` (per-callsign `PilotState`)
- geometry:  `airspace.json` (via `airspace.Airfield`)

No third-party Python dependencies: `http.server` for the server, Leaflet from
a CDN for the map. Run it from the bot with `--map-port`, or standalone:

    uv run map_server.py --airfield Kutaisi --port 8080
    uv run map_server.py --airfield Gudauta --replay debrief/tracks_*.json
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from airspace import Airfield
from brain import AtcBrain
from state_client import StateClient

WEB = Path(__file__).with_name("web")
OVERLAYS = WEB / "overlays"

# AI aircraft are sent within this radius of the airfield (bounds the payload);
# the map page then filters to the current viewport, so zoom/pan declutters.
AI_RADIUS_NM = 150.0

# How many chatter-log entries to keep for the map's log drawer.
CHATTER_LIMIT = 200

# Flight-path trails. A full flight (inbound call -> parked) can run 15-30 min,
# and this is a *debug* tool, so keep a very long history: at the ~2 s poll,
# 3600 points is ~2 hours. Nothing is time-expired — the trail stays until the
# aircraft leaves the mission (or the server restarts).
TRACK_MAX_POINTS = 3600
# Stop recording a trail once the aircraft is this far from the field (it is
# off-station; the trail would only clutter the map and waste memory).
TRACK_RADIUS_NM = 60.0
# Don't add a point unless the aircraft has moved at least this far, so a parked
# or orbiting aircraft does not fill the trail with near-identical points.
TRACK_MIN_MOVE_NM = 0.02
# How many radio calls to keep per aircraft (comm markers on the path).
COMM_MAX = 1000
# How often (seconds of wall clock) to autosave the track history when a
# `tracks_file` is configured. Cheap (a few KB of JSON at most).
TRACKS_SAVE_INTERVAL = 10.0

# Where saved sortie snapshots ("debriefs") live, and their filename pattern:
#   <DEBRIEF_DIR>/tracks_<airfield>_<YYYYMMDD-HHMMSS>.json
# A fresh file per session, so a tester can later pick the exact event.
DEBRIEF_DIR = Path(__file__).with_name("debrief")


def debrief_filename(airfield: str, when: float | None = None) -> str:
    """A timestamped debrief filename for an airfield (one file per session).

    Keyed by airfield + start time (not callsign: one file holds every
    callsign's trail). E.g. ``tracks_gudauta_20261010-105236.json``.
    """
    slug = "".join(c if c.isalnum() else "-" for c in airfield.lower()).strip("-")
    stamp = (time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
             if when is not None else time.strftime("%Y%m%d-%H%M%S"))
    return f"tracks_{slug}_{stamp}.json"


class TrackHistory:
    """Per-callsign flight-path trails + comm markers for the map.

    Keeps a bounded deque of recent positions per aircraft, plus the radio calls
    made at those positions (so the map can show *where* a pilot called and
    *where* the reply came). Recording stops beyond `radius_nm` (off-station)
    and points closer than `min_move_nm` are skipped, so the trail is a clean
    path rather than a cloud of dots.

    Trails are **retained** after an aircraft leaves the mission (or the pilot
    quits the slot): this is a debrief tool, so a completed sortie must stay
    reviewable — a pilot who logs off should still see their whole flight.
    Retained tracks are marked `active=False` in the snapshot and drawn dimmed;
    they are only ever removed by a `reset`, a server restart without a
    `tracks_file`, or (optionally) loading a saved file. See `save`/`load`.
    """

    def __init__(self, radius_nm: float = TRACK_RADIUS_NM,
                 max_points: int = TRACK_MAX_POINTS,
                 min_move_nm: float = TRACK_MIN_MOVE_NM,
                 comm_max: int = COMM_MAX):
        self.radius_nm = radius_nm
        self.max_points = max_points
        self.min_move_nm = min_move_nm
        self.comm_max = comm_max
        self._tracks: dict[str, deque[tuple[float, float, float]]] = {}
        self._comms: dict[str, deque[dict]] = {}
        self._active: set[str] = set()

    def update(self, callsign: str, lat: float, lon: float,
               distance_nm: float, alt_ft: float = 0.0) -> None:
        """Record a position for a callsign (skips off-station / tiny moves)."""
        self._active.add(callsign)
        if distance_nm > self.radius_nm:
            return  # off-station: stop recording
        track = self._tracks.get(callsign)
        if track is None:
            track = deque(maxlen=self.max_points)
            self._tracks[callsign] = track
        if track:
            plat, plon, _ = track[-1]
            if _nm_between(plat, plon, lat, lon) < self.min_move_nm:
                return  # barely moved
        track.append((lat, lon, alt_ft))

    def add_comm(self, callsign: str, lat: float, lon: float, kind: str,
                 text: str, controller: str = "", t: str = "") -> None:
        """Attach a radio call (or state event) to a position on the path."""
        comms = self._comms.get(callsign)
        if comms is None:
            comms = deque(maxlen=self.comm_max)
            self._comms[callsign] = comms
        comms.append({"lat": lat, "lon": lon, "kind": kind, "text": text,
                      "controller": controller, "t": t})

    def last_position(self, callsign: str) -> tuple[float, float] | None:
        track = self._tracks.get(callsign)
        return (track[-1][0], track[-1][1]) if track else None

    def path(self, callsign: str) -> list[list[float]]:
        return [[lat, lon, alt] for lat, lon, alt in self._tracks.get(callsign, ())]

    def comms(self, callsign: str) -> list[dict]:
        return list(self._comms.get(callsign, ()))

    def is_active(self, callsign: str) -> bool:
        return callsign in self._active

    def callsigns(self) -> list[str]:
        """Every callsign with a recorded trail (active or retained)."""
        return list(self._tracks)

    def point_count(self) -> int:
        """Total recorded points across every trail."""
        return sum(len(t) for t in self._tracks.values())

    def is_empty(self) -> bool:
        """True if no trail has any points (nothing worth saving)."""
        return self.point_count() == 0

    def forget(self, callsign: str) -> None:
        self._tracks.pop(callsign, None)
        self._comms.pop(callsign, None)
        self._active.discard(callsign)

    def set_active(self, active: set[str]) -> None:
        """Mark which callsigns are currently in the mission.

        Trails are **not** dropped for the ones no longer present — they are
        retained for debrief. This only flips the `active` flag used to dim
        completed trails on the map.
        """
        self._active = set(active)

    def to_json(self) -> dict:
        """Serialisable snapshot of every trail + comm marker."""
        return {
            "tracks": {cs: [list(p) for p in pts]
                       for cs, pts in self._tracks.items()},
            "comms": {cs: list(c) for cs, c in self._comms.items()},
        }

    def load_json(self, data: dict) -> None:
        """Merge a `to_json()` snapshot (used to restore a saved debrief)."""
        if not isinstance(data, dict):
            return
        for cs, pts in (data.get("tracks") or {}).items():
            track = deque(maxlen=self.max_points)
            for p in pts:
                try:
                    track.append((float(p[0]), float(p[1]),
                                  float(p[2]) if len(p) > 2 else 0.0))
                except (TypeError, ValueError, IndexError):
                    continue
            if track:
                self._tracks[cs] = track
        for cs, calls in (data.get("comms") or {}).items():
            comms = deque(maxlen=self.comm_max)
            for c in calls:
                if isinstance(c, dict):
                    comms.append(c)
            if comms:
                self._comms[cs] = comms

    def save(self, path: Path) -> None:
        """Write the tracks to `path` atomically (temp file + rename)."""
        self.save_json(self.to_json(), path)

    def save_json(self, data: dict, path: Path) -> None:
        """Write a debrief payload (tracks + optional chatter) atomically."""
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except OSError:
            pass  # a debrief file is best-effort; never take the map down

    def load(self, path: Path) -> None:
        """Restore tracks from `path`, if it exists and parses."""
        try:
            if path.exists():
                self.load_json(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass

    @staticmethod
    def read_chatter(path: Path) -> list[dict]:
        """The saved radio log from a debrief file (empty if none/absent)."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        return [c for c in (data.get("chatter") or []) if isinstance(c, dict)]


def _nm_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in NM (small helper; avoids importing airspace)."""
    import math
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a)) / 1852.0


def _overlay_payload(airfield: Airfield) -> dict | None:
    """Georeferenced chart overlay for an airfield, if one has been generated.

    `georef.py` writes `<name>.png` (north-up) + `<name>.json` (lat/lon bounds)
    into `web/overlays/`. Returns the URL + bounds for a Leaflet imageOverlay.
    """
    name = airfield.name.lower()
    png = OVERLAYS / f"{name}.png"
    meta = OVERLAYS / f"{name}.json"
    if not (png.exists() and meta.exists()):
        return None
    bounds = json.loads(meta.read_text())
    return {
        "url": f"/overlays/{name}.png",
        "bounds": [[bounds["south"], bounds["west"]],
                   [bounds["north"], bounds["east"]]],
    }


def _airspace_payload(airfield: Airfield, basemap: str | None = None) -> dict:
    """Static geometry for the map (CTR, gates, runways, taxi, parking)."""
    return {
        "name": airfield.name,
        "tower": airfield.tower,
        "elevation_ft": airfield.elevation_ft,
        "active_runway": airfield.active_runway,
        "center": [airfield.ctr.center_lat, airfield.ctr.center_lon],
        "ctr": {
            "ceiling_ft_agl": airfield.ctr.ceiling_ft_agl,
            "polygon": [list(p) for p in airfield.ctr.polygon_latlon],
        },
        "gates": {name: list(pos) for name, pos in airfield.gates.items()},
        "runways": {name: list(pos) for name, pos in airfield.runways.items()},
        "taxi_routes": airfield.taxi_routes,
        "parking_routes": airfield.parking_routes,
        "parking_areas": {name: list(pos) for name, pos in airfield.parking_areas.items()},
        "holding_points": {name: list(pos) for name, pos in airfield.holding_points.items()},
        # The areas the bot actually checks, derived from the same parameters
        # the brain uses (so the map can't drift from the logic).
        "checks": {
            "holding": airfield.holding_zone_geometry(),
            "final": airfield.final_zone_geometry(),
            "runway": airfield.runway_corridor_geometry(),
        },
        "overlay": _overlay_payload(airfield),
        # Base map layer (OSM or DCS tiles) chosen by config/CLI.
        "basemap": _basemap_payload(airfield, basemap),
        # Agencies for the chatter-log filter checkboxes (shown even before
        # they have transmitted, so the filter list is stable).
        "agencies": _agencies(airfield),
    }


def _basemap_payload(airfield: Airfield, choice: str | None) -> dict:
    """The base-map tile layer for the map.

    Reads `airspace.json` `map.layers` (with a fallback), picks `choice`
    (CLI --base-map) or `map.base` (config) or 'osm'. Returns the Leaflet
    tileLayer options the page needs: url, attribution, maxZoom,
    maxNativeZoom, tms.
    """
    cfg = airfield.map_config or {}
    layers = cfg.get("layers") or {}
    name = choice or cfg.get("base") or "osm"
    spec = layers.get(name) or _FALLBACK_LAYERS.get(name) or _FALLBACK_LAYERS["osm"]
    return {
        "name": name,
        "url": spec["url"],
        "attribution": spec.get("attribution", ""),
        "maxZoom": spec.get("maxZoom", 19),
        "maxNativeZoom": spec.get("maxNativeZoom", spec.get("maxZoom", 19)),
        "tms": bool(spec.get("tms", False)),
    }


# Used when airspace.json has no `map` section.
_FALLBACK_LAYERS = {
    "osm": {"url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "&copy; OpenStreetMap contributors", "maxZoom": 19},
    "dcs": {"url": "http://dcsmaps.com/caucasus/{z}/{x}/{y}.png",
            "attribution": "&copy; dcsmaps.com", "maxZoom": 16,
            "maxNativeZoom": 12, "tms": True},
}


def _agencies(airfield: Airfield) -> list[str]:
    """Radio agencies for the chatter filter: ground/tower/control/atis."""
    out = []
    if airfield.ground_frequency_mhz:
        out.append("ground")
    if airfield.frequency_mhz:
        out.append("tower")
    if airfield.control_frequency_mhz:
        out.append("control")
    if airfield.atis_frequency_mhz:
        out.append("atis")
    return out


class MapService:
    """Builds the JSON the map page polls, from live state + brain phases."""

    def __init__(self, airfield: Airfield, brain: AtcBrain,
                 state: StateClient | None, lock: threading.RLock | None = None,
                 basemap: str | None = None,
                 tracks_file: Path | str | None = None,
                 replay: bool = False):
        self.airfield = airfield
        self.brain = brain
        self.state = state
        self.lock = lock or threading.RLock()
        self.basemap = basemap  # base-map layer name override (else config)
        # Chatter log: recent radio traffic (rx + tx) for the map's log drawer.
        self._chatter: deque[dict] = deque(maxlen=CHATTER_LIMIT)
        self._chatter_lock = threading.Lock()
        # Flight-path trails (per callsign), for the map's path overlay.
        self.tracks = TrackHistory()
        # Optional debrief file: restore saved trails on startup and autosave
        # them periodically, so a sortie survives a bot/map restart. When set
        # and the file already exists it is a *replay*: we restore it and, by
        # default, stop recording/overwriting, so a tester can review a saved
        # event (see `replay`).
        self.tracks_file = Path(tracks_file) if tracks_file else None
        self._last_saved = 0.0
        existed = self.tracks_file is not None and self.tracks_file.exists()
        if existed:
            self.tracks.load(self.tracks_file)
            # Restore the saved radio log too, so the chatter window mirrors a
            # replayed sortie (older files simply have none).
            self._chatter.extend(TrackHistory.read_chatter(self.tracks_file))
        # `replay` true forces read-only review; when a tracks_file names an
        # existing file we default to replay too (don't clobber a saved sortie).
        self.replay = bool(replay or existed)
        # Last seen phase per callsign, to record phase transitions on the path.
        self._last_phase: dict[str, str] = {}

    def save_tracks(self) -> None:
        """Write the current trails to `tracks_file` (no-op if unconfigured).

        An empty history is **not** written, so a run that captured no trail
        never leaves an empty debrief file behind (which would show up in the
        map's load dropdown as an unusable entry).
        """
        if self.tracks_file is not None:
            with self.lock:
                if self.tracks.is_empty():
                    return
                data = self.tracks.to_json()
                # Include the radio log, so a replay restores the chatter
                # window too (not just the trails).
                with self._chatter_lock:
                    data["chatter"] = list(self._chatter)
                self.tracks.save_json(data, self.tracks_file)

    def _maybe_save_tracks(self) -> None:
        """Autosave the trails at most every `TRACKS_SAVE_INTERVAL` seconds."""
        if self.tracks_file is None or self.replay:
            return
        now = time.monotonic()
        if now - self._last_saved < TRACKS_SAVE_INTERVAL:
            return
        self._last_saved = now
        self.save_tracks()

    def record(self, players) -> None:
        """Record positions + phase events for the live players (no map needed).

        Called on the bot's monitor tick so the debrief trail is captured whether
        or not a browser is polling the map. `players` is the list of live
        `Aircraft`. Idempotent with `snapshot`, which records the same way when
        the map *is* open. A no-op while reviewing a saved replay.
        """
        if self.replay:
            return
        with self.lock:
            for ac in players:
                callsign = self.brain.callsign_for_speaker(ac.player)
                pilot = self.brain.pilots.get(callsign)
                distance = self.airfield.ctr.distance_nm(ac.lat, ac.lon)
                self.tracks.update(callsign, ac.lat, ac.lon, distance, ac.alt_ft)
                phase = pilot.phase.value if pilot else "Unknown"
                if pilot is not None and self._last_phase.get(callsign) != phase:
                    self._last_phase[callsign] = phase
                    self.tracks.add_comm(
                        callsign, ac.lat, ac.lon, "state", phase,
                        pilot.last_controller, time.strftime("%H:%M:%S"))
            self.tracks.set_active(
                {self.brain.callsign_for_speaker(ac.player) for ac in players})
        self._maybe_save_tracks()

    def log_chatter(self, kind: str, freq_mhz: float, who: str,
                    text: str, controller: str = "") -> None:
        """Record one radio event (kind: 'rx' pilot, 'tx' ATC, 'sys')."""
        if self.replay:
            return  # reviewing a saved sortie: keep its log, don't add live ones
        # Resolve the flight callsign for a pilot transmission (the brain learns
        # SRS-name -> callsign from what the pilot says), so the log can show
        # "Colt 1" instead of the raw DCS/SRS name. Empty for ATC/system lines.
        callsign = ""
        if kind == "rx" and who:
            with self.lock:
                callsign = self.brain.callsign_for_speaker(who)
            if callsign == who:
                callsign = ""  # not learned yet; don't duplicate the name
        elif kind == "tx":
            # An ATC reply always opens with the callsign; use it to pin the
            # reply to the pilot's position on the path.
            with self.lock:
                callsign = self.brain.callsigns.extract(text) or ""
        stamp = time.strftime("%H:%M:%S")
        entry = {
            "t": stamp,
            "kind": kind,
            "freq": round(freq_mhz, 3),
            "who": who,
            "callsign": callsign,
            "text": text,
            "controller": controller,
        }
        with self._chatter_lock:
            self._chatter.append(entry)
        # Attach the call to the aircraft's path, so the map can show *where*
        # the pilot called and *where* the reply came. ATC replies (tx) are
        # pinned to the pilot's last known position.
        #
        # A pilot transmission that could not be tied to a callsign (the first
        # call before the brain learned the speaker, or a garbled/understood-less
        # call) is still attached — under the raw speaker name, which is how the
        # trail is keyed until the mapping is learned. This keeps *every* spoken
        # transmission on the trail, not just the recognised ones.
        key = callsign
        if not key and kind == "rx" and who:
            key = who
        if key:
            pos = self.tracks.last_position(key)
            if pos is None and who and who != key:
                pos = self.tracks.last_position(who)
            if pos is not None:
                self.tracks.add_comm(key, pos[0], pos[1], kind, text,
                                     controller, stamp)

    def chatter(self) -> list[dict]:
        with self._chatter_lock:
            return list(self._chatter)

    def list_debriefs(self) -> list[dict]:
        """Saved debrief files that contain a trail, newest first.

        Empty files (no recorded points) are skipped: they are unusable as a
        replay and would only clutter the map's load dropdown.
        """
        out: list[dict] = []
        for path in DEBRIEF_DIR.glob("tracks_*.json"):
            try:
                if not json.loads(path.read_text()).get("tracks"):
                    continue
                out.append({"name": path.name,
                            "mtime": path.stat().st_mtime})
            except (OSError, ValueError):
                continue
        out.sort(key=lambda d: d["mtime"], reverse=True)
        for d in out:
            d["time"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(d["mtime"]))
        return out

    def load_debrief(self, name: str) -> dict:
        """Load a saved debrief by filename and switch to read-only replay.

        Only a bare filename inside `DEBRIEF_DIR` is accepted (no path
        traversal), so the web endpoint can never read an arbitrary file.
        Returns a small status dict; on error, `ok` is False and nothing
        changes.
        """
        base = Path(name).name  # strip any directory components
        if not base or base != name or not base.endswith(".json"):
            return {"ok": False, "error": "invalid name"}
        path = (DEBRIEF_DIR / base).resolve()
        if path.parent != DEBRIEF_DIR.resolve() or not path.is_file():
            return {"ok": False, "error": "not found"}
        loaded = TrackHistory()
        loaded.load(path)
        if loaded.is_empty():
            return {"ok": False, "error": "empty debrief — no trail was recorded"}
        # Restore the saved radio log too, so the chatter window mirrors the
        # replayed sortie (falls back to empty for older files without it).
        chatter = TrackHistory.read_chatter(path)
        with self.lock:
            self.tracks = loaded
            self._last_phase = {}
            with self._chatter_lock:
                self._chatter.clear()
                self._chatter.extend(chatter)
        self.tracks_file = path
        self.replay = True  # freeze: don't record or overwrite the saved file
        return {"ok": True, "name": base}

    def unload_debrief(self) -> dict:
        """Leave replay and resume live recording (clears the loaded trail)."""
        with self.lock:
            self.tracks = TrackHistory()
            self._last_phase = {}
            with self._chatter_lock:
                self._chatter.clear()
        self.tracks_file = None
        self.replay = False
        return {"ok": True}

    @staticmethod
    def _retained_entry(callsign: str, path: list, comms: list) -> dict:
        """A trail shown with no live aircraft (completed, or a replay)."""
        return {
            "callsign": callsign,
            "player": "",
            "type": "",
            "lat": None,
            "lon": None,
            "alt_ft": None,
            "heading": None,
            "coalition": 0,
            "phase": "",
            "controller": "",
            "entry_gate": "",
            "exit_gate": "",
            "active": False,
            "path": path,
            "comms": comms,
        }

    def snapshot(self) -> dict:
        """One map frame: airspace geometry + live aircraft with their state."""
        aircraft: list[dict] = []
        ai_air: list[dict] = []
        error = None
        active: set[str] = set()
        if self.state is not None and not self.replay:
            try:
                players = self.state.aircraft()
                all_units = self.state.all_units()
            except (OSError, RuntimeError) as exc:
                players, all_units = [], []
                error = str(exc)
            with self.lock:
                for ac in players:
                    callsign = self.brain.callsign_for_speaker(ac.player)
                    pilot = self.brain.pilots.get(callsign)
                    distance = self.airfield.ctr.distance_nm(ac.lat, ac.lon)
                    self.tracks.update(callsign, ac.lat, ac.lon, distance,
                                       ac.alt_ft)
                    phase = pilot.phase.value if pilot else "Unknown"
                    # Record a phase transition as a timeline event, so the path
                    # shows *when* the brain changed state (not just the calls).
                    if pilot is not None and self._last_phase.get(callsign) != phase:
                        self._last_phase[callsign] = phase
                        self.tracks.add_comm(
                            callsign, ac.lat, ac.lon, "state", phase,
                            pilot.last_controller, time.strftime("%H:%M:%S"))
                    aircraft.append({
                        "callsign": callsign,
                        "player": ac.player,
                        "type": ac.type,
                        "lat": ac.lat,
                        "lon": ac.lon,
                        "alt_ft": round(ac.alt_ft),
                        "heading": round(ac.heading),
                        "coalition": ac.coalition,
                        "phase": phase,
                        "controller": pilot.last_controller if pilot else "",
                        "entry_gate": pilot.entry_gate if pilot else "",
                        "exit_gate": pilot.exit_gate if pilot else "",
                        "active": True,
                        "path": self.tracks.path(callsign),
                        "comms": self.tracks.comms(callsign),
                    })
                # Mark which aircraft are present, so the map can dim the rest.
                # Trails are *retained* (not pruned) for debrief after a pilot
                # leaves the mission or quits the slot.
                active = {self.brain.callsign_for_speaker(ac.player)
                          for ac in players}
                self.tracks.set_active(active)
                for callsign in list(self._last_phase):
                    if callsign not in active:
                        del self._last_phase[callsign]
            # AI air traffic (planes/helicopters) near the field, for
            # situational awareness. The map page filters these to the current
            # viewport, so zoom/pan declutters without a server-side radius.
            for ac in all_units:
                if ac.player or ac.category not in (0, 1):
                    continue
                if self.airfield.ctr.distance_nm(ac.lat, ac.lon) > AI_RADIUS_NM:
                    continue
                ai_air.append({
                    "callsign": ac.callsign,
                    "type": ac.type,
                    "lat": ac.lat,
                    "lon": ac.lon,
                    "alt_ft": round(ac.alt_ft),
                    "heading": round(ac.heading),
                    "coalition": ac.coalition,
                })
        # Retained trails: completed sorties (still visible after a pilot logs
        # off) and — with no live state — a loaded replay, so a saved event can
        # be reviewed on the map without DCS running.
        with self.lock:
            for callsign in self.tracks.callsigns():
                if callsign in active:
                    continue
                aircraft.append(self._retained_entry(
                    callsign, self.tracks.path(callsign),
                    self.tracks.comms(callsign)))
        self._maybe_save_tracks()  # keep the debrief file reasonably fresh
        return {
            "airfield": _airspace_payload(self.airfield, self.basemap),
            "aircraft": aircraft,
            "ai_air": ai_air,
            "chatter": self.chatter(),
            "error": error,
            "replay": self.replay,
            "replay_name": (self.tracks_file.name
                            if self.replay and self.tracks_file else ""),
        }


def make_handler(service: MapService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the bot log clean
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The browser closed the tab / refreshed mid-response. Normal
                # for a polling client; nothing to do but drop the connection.
                self.close_connection = True

        def do_GET(self) -> None:
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # client hung up; not an error

        def do_POST(self) -> None:
            try:
                self._route_post()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                return {}

        def _route_post(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/debrief/load":
                result = service.load_debrief(self._json_body().get("name", ""))
            elif path == "/api/debrief/unload":
                result = service.unload_debrief()
            else:
                self.send_error(404)
                return
            body = json.dumps(result).encode()
            self._send(200 if result.get("ok") else 400, body,
                       "application/json; charset=utf-8")

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/atc":
                body = json.dumps(service.snapshot(), separators=(",", ":")).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/debriefs":
                body = json.dumps({"debriefs": service.list_debriefs()}).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            files = {
                "/": (WEB / "index.html", "text/html; charset=utf-8"),
                "/map.js": (WEB / "map.js", "text/javascript; charset=utf-8"),
                "/map.css": (WEB / "map.css", "text/css; charset=utf-8"),
            }
            if path.startswith("/overlays/") and path.endswith(".png"):
                overlay = OVERLAYS / Path(path).name
                if overlay.exists():
                    self._send(200, overlay.read_bytes(), "image/png")
                    return
            if path not in files:
                self.send_error(404)
                return
            file_path, content_type = files[path]
            self._send(200, file_path.read_bytes(), content_type)

    return Handler


def start_map_server(airfield: Airfield, brain: AtcBrain,
                     state: StateClient | None, port: int,
                     host: str = "0.0.0.0",
                     lock: threading.RLock | None = None,
                     log: Callable[[str], None] = print,
                     basemap: str | None = None,
                     tracks_file: Path | str | None = None,
                     replay: bool = False,
                     ) -> tuple[ThreadingHTTPServer | None, MapService]:
    """Start the map server on a daemon thread.

    Returns (server, service). `server` is None (and a warning is logged) if the
    port is already in use, so a busy map port never takes the ATC bot down.
    The `service` is returned so the bot can push chatter-log entries to it.

    `tracks_file`, when set, restores saved trails on startup and autosaves them
    during the run (plus once at process exit), so a completed sortie survives a
    bot restart for after-action review.
    """
    service = MapService(airfield, brain, state, lock, basemap=basemap,
                         tracks_file=tracks_file, replay=replay)
    if tracks_file is not None:
        if service.replay:
            log(f"[*] Debrief replay (read-only): {Path(tracks_file)}")
        else:
            import atexit
            atexit.register(service.save_tracks)
            log(f"[*] Flight-track debrief file: {Path(tracks_file)}")
    try:
        server = ThreadingHTTPServer((host, port), make_handler(service))
    except OSError as error:
        log(f"[!] Map view disabled: cannot bind {host}:{port} ({error})")
        return None, service
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="atc-map")
    thread.start()
    log(f"[*] Map view on http://{host}:{port}/  (airfield {airfield.name})")
    return server, service


def main() -> None:
    import argparse

    from airspace import Airspace

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airfield", default="Kutaisi")
    parser.add_argument("--airspace", default=None)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--state-host", default="127.0.0.1")
    parser.add_argument("--state-port", type=int, default=10309)
    parser.add_argument("--no-state", action="store_true")
    parser.add_argument("--base-map", default=None,
                        help="base map layer: 'dcs' (DCS tiles) or 'osm' "
                             "(default: airspace.json map.base)")
    parser.add_argument("--tracks-file", default=None,
                        help="save/restore flight trails here (debrief). "
                             "Set automatically with --debug.")
    parser.add_argument("--replay", default=None,
                        help="load a saved debrief file and review it read-only")
    parser.add_argument("--list-debriefs", action="store_true",
                        help="list saved debrief files and exit")
    parser.add_argument("--debug", action="store_true",
                        help="save flight trails for after-action review")
    args = parser.parse_args()

    if args.list_debriefs:
        for path in sorted(DEBRIEF_DIR.glob("tracks_*.json")):
            print(path)
        return

    tracks_file = args.replay or args.tracks_file
    if tracks_file is None and args.debug:
        DEBRIEF_DIR.mkdir(parents=True, exist_ok=True)
        tracks_file = DEBRIEF_DIR / debrief_filename(args.airfield)

    airspace = Airspace.load(args.airspace) if args.airspace else Airspace.load()
    airfield = airspace.get(args.airfield)
    if airfield is None:
        raise SystemExit(f"airfield {args.airfield!r} not found in airspace.json")
    brain = AtcBrain(tower=airfield.tower, runway=airfield.active_runway,
                     ground=airfield.ground, control=airfield.control,
                     gates=list(airfield.gates), gate_locator=airfield.nearest_gate,
                     airfield=airfield)
    state = None if args.no_state else StateClient(args.state_host, args.state_port)
    server, _service = start_map_server(airfield, brain, state, args.port, args.host,
                                        basemap=args.base_map,
                                        tracks_file=tracks_file,
                                        replay=bool(args.replay))
    if server is None:
        raise SystemExit("map server could not start")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _service.save_tracks()


if __name__ == "__main__":
    main()
