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
"""

from __future__ import annotations

import json
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


def _airspace_payload(airfield: Airfield) -> dict:
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
        # Agencies for the chatter-log filter checkboxes (shown even before
        # they have transmitted, so the filter list is stable).
        "agencies": _agencies(airfield),
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
                 state: StateClient | None, lock: threading.RLock | None = None):
        self.airfield = airfield
        self.brain = brain
        self.state = state
        self.lock = lock or threading.RLock()
        # Chatter log: recent radio traffic (rx + tx) for the map's log drawer.
        self._chatter: deque[dict] = deque(maxlen=CHATTER_LIMIT)
        self._chatter_lock = threading.Lock()

    def log_chatter(self, kind: str, freq_mhz: float, who: str,
                    text: str, controller: str = "") -> None:
        """Record one radio event (kind: 'rx' pilot, 'tx' ATC, 'sys')."""
        entry = {
            "t": time.strftime("%H:%M:%S"),
            "kind": kind,
            "freq": round(freq_mhz, 3),
            "who": who,
            "text": text,
            "controller": controller,
        }
        with self._chatter_lock:
            self._chatter.append(entry)

    def chatter(self) -> list[dict]:
        with self._chatter_lock:
            return list(self._chatter)

    def snapshot(self) -> dict:
        """One map frame: airspace geometry + live aircraft with their state."""
        aircraft: list[dict] = []
        ai_air: list[dict] = []
        error = None
        if self.state is not None:
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
                    aircraft.append({
                        "callsign": callsign,
                        "player": ac.player,
                        "type": ac.type,
                        "lat": ac.lat,
                        "lon": ac.lon,
                        "alt_ft": round(ac.alt_ft),
                        "heading": round(ac.heading),
                        "coalition": ac.coalition,
                        "phase": pilot.phase.value if pilot else "Unknown",
                        "controller": pilot.last_controller if pilot else "",
                        "entry_gate": pilot.entry_gate if pilot else "",
                        "exit_gate": pilot.exit_gate if pilot else "",
                    })
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
        return {
            "airfield": _airspace_payload(self.airfield),
            "aircraft": aircraft,
            "ai_air": ai_air,
            "chatter": self.chatter(),
            "error": error,
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

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/atc":
                body = json.dumps(service.snapshot(), separators=(",", ":")).encode()
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
                     ) -> tuple[ThreadingHTTPServer | None, MapService]:
    """Start the map server on a daemon thread.

    Returns (server, service). `server` is None (and a warning is logged) if the
    port is already in use, so a busy map port never takes the ATC bot down.
    The `service` is returned so the bot can push chatter-log entries to it.
    """
    service = MapService(airfield, brain, state, lock)
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
    args = parser.parse_args()

    airspace = Airspace.load(args.airspace) if args.airspace else Airspace.load()
    airfield = airspace.get(args.airfield)
    if airfield is None:
        raise SystemExit(f"airfield {args.airfield!r} not found in airspace.json")
    brain = AtcBrain(tower=airfield.tower, runway=airfield.active_runway,
                     ground=airfield.ground, control=airfield.control,
                     gates=list(airfield.gates), gate_locator=airfield.nearest_gate,
                     airfield=airfield)
    state = None if args.no_state else StateClient(args.state_host, args.state_port)
    server, _service = start_map_server(airfield, brain, state, args.port, args.host)
    if server is None:
        raise SystemExit("map server could not start")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
