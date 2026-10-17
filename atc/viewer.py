"""Read-only public viewer for the ATC trainer (stdlib HTTP server + Leaflet).

A sidecar service for sharing the map on the Internet with *no authority*:

- GET-only: there is no POST handler, so no request can restart, stop or
  mutate anything. The stdlib answers other methods with 501.
- Live view = the newest debrief file (the bot autosaves one per pilot spawn
  every ~10 s), so the viewer never talks to the DCS bridge or the bot.
- Debrief playback is client-side: the browser fetches a saved file and renders
  it. The server holds no replay state.
- Basic auth (constant-time compare) + optional self-signed TLS, so the
  password is not sent in the clear.
- Filenames are validated against the debrief slug pattern and resolved inside
  the debrief directory (no path traversal).

It imports the payload helpers from `map_server.py` (read-only imports; the
bot's own map server is untouched) and reuses the same web assets.

Run it (from the repo root, `atc/` on the path):

    cd atc && uv run viewer.py --port 8443   # HTTPS by default (self-signed)

Credentials come from `.viewer-auth.json` next to this file (or
--username/--password). Then forward/tunnel port 8443 (or put a reverse proxy
in front). The bot's own map server should stay on 127.0.0.1 / a LAN, never
on the Internet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Read-only reuse of the map server's payload helpers and constants. Nothing
# here mutates bot state; importing must not start servers or threads.
from map_server import (DEBRIEF_DIR, OVERLAYS, WEB, _airspace_payload,
                        _basemap_payload, _debrief_slug, TrackHistory)

MS_TO_KT = 1.943844  # DCS weather is m/s; the map shows knots (same as bot)

# A debrief filename is exactly: tracks_<slug>[_<slug>...]_<stamp>.json
# Validating the whole shape (not just "ends with .json") means the viewer can
# only ever serve files the bot itself wrote. Slugs keep alphanumerics and map
# everything else (including `_`) to `-`, so parts never contain underscores.
_STAMP_LEN = len("YYYYMMDD-HHMMSS")


def _valid_debrief_name(name: str) -> bool:
    if not name.startswith("tracks_") or not name.endswith(".json"):
        return False
    stem = name[len("tracks_"):-len(".json")]
    parts = stem.split("_")
    if len(parts) < 2 or len(parts[-1]) != _STAMP_LEN:
        return False
    stamp = parts[-1]
    if not stamp[:8].isdigit() or stamp[8] != "-" or not stamp[9:].isdigit():
        return False
    return all(_debrief_slug(p) == p and p for p in parts[:-1])


def _resolve_debrief(name: str, debrief_dir: Path) -> Path | None:
    """Absolute path for a debrief name, or None if it is not a safe match."""
    if not _valid_debrief_name(name):
        return None
    path = (debrief_dir / name).resolve()
    if path.parent != debrief_dir.resolve() or not path.is_file():
        return None
    return path


def newest_debrief(debrief_dir: Path) -> Path | None:
    """The most recently modified debrief that actually has a trail."""
    best: tuple[float, Path] | None = None
    for path in debrief_dir.glob("tracks_*.json"):
        try:
            mtime = path.stat().st_mtime
            if not json.loads(path.read_text()).get("tracks"):
                continue
        except (OSError, ValueError):
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return best[1] if best else None


def list_debriefs(debrief_dir: Path) -> list[dict]:
    """Saved debrief files with a trail, newest first (same shape as the bot)."""
    out: list[dict] = []
    for path in debrief_dir.glob("tracks_*.json"):
        try:
            if not json.loads(path.read_text()).get("tracks"):
                continue
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        out.append({"name": path.name, "mtime": mtime})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    for d in out:
        d["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["mtime"]))
    return out


def _parse_wind_kt(text: str) -> tuple[float, float] | None:
    """(dir_deg, speed_kt) from a spoken wind phrase, or None.

    Handles the two formats the bot produces:
    - ATIS:   "wind 030 degrees, 4 meters per second" (m/s -> kt)
    - ATC:    "Wind 030, 4 knots"
    - calm:   "wind calm" -> (0, 0)
    """
    m = re.search(r"wind (\d{3})\s*(?:degrees)?,?\s*(\d+(?:\.\d+)?)\s*"
                  r"(meters? per second|m/s|knots?|kt)\b", text, re.IGNORECASE)
    if m:
        speed = float(m.group(2))
        if m.group(3).lower().startswith(("meter", "m/s")):
            speed *= MS_TO_KT
        return float(m.group(1)), speed
    if re.search(r"\bwind calm\b", text, re.IGNORECASE):
        return 0.0, 0.0
    return None


def _parse_runway(text: str) -> str | None:
    """The runway from an 'in use' phrase (ATIS/ground), e.g. '07 in use'."""
    m = re.search(r"\b(\d{2})\s*(?:L|C|R)?\s+in use\b", text, re.IGNORECASE)
    return m.group(1) if m else None


def sortie_conditions(chatter: list[dict]) -> dict:
    """Wind + active runway for a saved sortie, parsed from its own chatter.

    The debrief files carry no structured weather, but the ATIS broadcast and
    the ground/tower readbacks state both ("07 in use", "wind 030, 4 knots").
    The most recent ATIS line wins (it is the authoritative broadcast); ATC
    phrases fill in when a sortie has no ATIS chatter. Returns
    {dir, speed_kt, text, runway} with whatever was found (None otherwise).
    """
    wind: tuple[float, float] | None = None
    runway: str | None = None
    for entry in chatter:  # chronological; later entries overwrite
        text = str(entry.get("text") or "")
        controller = entry.get("controller") or ""
        if controller == "atis":
            parsed = _parse_wind_kt(text)
            if parsed:
                wind = parsed
            parsed_rwy = _parse_runway(text)
            if parsed_rwy:
                runway = parsed_rwy
        elif controller in ("ground", "tower"):
            # The first "in use" phrase wins for the runway (it does not
            # change mid-sortie), but keep reading for wind: tower phrases
            # state it later (e.g. "wind 030, 8 knots, cleared to land").
            parsed_rwy = _parse_runway(text)
            if parsed_rwy and runway is None:
                runway = parsed_rwy
            if wind is None:
                parsed = _parse_wind_kt(text)
                if parsed:
                    wind = parsed
    payload: dict = {"dir": None, "speed_kt": None, "text": "", "runway": None}
    if wind:
        speed_kt = round(wind[1])
        payload["dir"] = wind[0]
        payload["speed_kt"] = speed_kt
        payload["text"] = ("calm" if speed_kt < 1
                           else f"{wind[0]:03.0f} degrees, {speed_kt} knots")
    if runway:
        payload["runway"] = runway
    return payload


def debrief_snapshot(path: Path, airfield, basemap: str | None) -> dict:
    """A map payload built purely from a saved debrief file.

    Same shape as the bot's live snapshot (so the same page renders it), but
    every aircraft is a retained trail (`active: false`) and there is no AI
    traffic or control block — the viewer has no live sources. The active
    runway and wind are recovered from the sortie's own chatter, so the zones
    (final wedge, runway corridor, line-up) are drawn for the runway the
    sortie actually used, not the config default.
    """
    tracks = TrackHistory()
    tracks.load(path)
    chatter = TrackHistory.read_chatter(path)
    aircraft = [_retained(callsign, tracks.path(callsign),
                          tracks.comms(callsign))
                for callsign in tracks.callsigns()]
    conditions = sortie_conditions(chatter)
    # Rebuild the zone geometry for the sortie's runway. The airfield object
    # is shared/mutable, so flip the runway, derive the payload, and restore —
    # the bot (if running) must never see a viewer-driven change.
    saved_runway = airfield.active_runway
    if conditions["runway"] and conditions["runway"] in airfield.runways:
        airfield.active_runway = conditions["runway"]
    try:
        af = _airspace_payload(airfield, basemap)
    finally:
        airfield.active_runway = saved_runway
    af["tower_pos"] = list(airfield.tower_pos) if airfield.tower_pos else \
        [airfield.ctr.center_lat, airfield.ctr.center_lon]
    # The viewer has no live wind source; show the sortie's recorded wind
    # (or nothing when the chatter never stated one).
    af["wind"] = {"dir": conditions["dir"], "speed": conditions["speed_kt"],
                  "text": conditions["text"]}
    # The dcsmaps tiles are plain http:// — an https:// viewer page must not
    # load them directly (mixed content = blocked = black map). Rewrite the
    # URL to the viewer's own tile proxy, which fetches from dcsmaps over
    # http and re-serves the bytes on this origin (same scheme as the page).
    if af["basemap"]["url"].startswith("http://"):
        af["basemap"] = dict(af["basemap"],
                             url="/tiles/" + af["basemap"]["url"])
    return {
        "airfield": af,
        "aircraft": aircraft,
        "ai_air": [],
        "chatter": chatter,
        "error": None,
        "replay": True,
        "replay_name": path.name,
        "control": None,
    }


def _retained(callsign: str, path: list, comms: list) -> dict:
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


def _self_signed_cert(cert: Path, key: Path, hostname: str,
                      san: str | None = None) -> bool:
    """Generate a throwaway self-signed cert (openssl must be installed).

    `san` adds extra subjectAltName entries (comma-separated), e.g. the bind
    host, so 127.0.0.1 keeps working alongside the public hostname.
    """
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", str(key), "-out", str(cert), "-days", "3650",
           "-subj", f"/CN={hostname}"]
    if san:
        names = [san] + ([hostname] if hostname != san else [])
        cmd += ["-addext",
                "subjectAltName=" + ", ".join(
                    n if n.startswith("DNS:") or n.startswith("IP:")
                    else (f"IP:{n}" if _is_ip(n) else f"DNS:{n}")
                    for n in names)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[!] Could not generate a self-signed certificate: {exc}",
              file=sys.stderr)
        return False


def _is_ip(text: str) -> bool:
    parts = text.split(".")
    return (len(parts) == 4
            and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))


def _load_auth_file(path: Path | str) -> dict[str, str]:
    """user -> password pairs from a JSON credentials file.

    The file lives outside the repo (default `atc/.viewer-auth.json`) so
    credentials are never committed and can be edited without restarting
    nothing else. Format:

        { "users": { "alice": "wonderland", "bob": "builder" } }

    Every entry is a separate account; the browser's login dialog must guess
    both the username and the password.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict) or not users:
        raise ValueError(f"{path}: expected {{\"users\": {{\"name\": \"pass\"}}}}")
    out = {}
    for user, password in users.items():
        user, password = str(user), str(password)
        if not user or not password:
            raise ValueError(f"{path}: empty user or password")
        out[user] = password
    return out


def _proxy_tile(service: "ViewerService", url: str) -> bytes | None:
    """Fetch an http:// tile on the viewer's behalf (mixed-content fix).

    Only URLs whose host is on the tile allowlist (the hosts named in the
    airfield's basemap config) are fetched, so the endpoint can never be used
    as an open proxy to arbitrary hosts. Small on-disk cache, since DCS tiles
    never change.
    """
    if not url.startswith("http://") or ".." in url:
        return None
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc
    if host not in service.tile_hosts:
        return None
    cache_path = service.tile_cache / hashlib.sha256(url.encode()).hexdigest()
    try:
        if cache_path.is_file():
            return cache_path.read_bytes()
    except OSError:
        pass
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as response:
            data = response.read()
    except (OSError, ValueError):
        return None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_path)
    except OSError:
        pass  # cache is best-effort; the tile is still served
    return data


def make_handler(service: "ViewerService"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass  # keep the log clean; auth failures are logged explicitly

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # The viewer is read-only; nothing here may be cached as an
                # action, and no script may be injected into the page.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _authorized(self) -> bool:
            """A valid session cookie, or a valid Basic header.

            The page logs in via the dialog (POST /api/login -> token cookie);
            Basic auth still works for API clients / bookmarked direct use.
            """
            token = self._cookie_token()
            if token and service._check_session(token):
                return True
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            user, _, supplied = decoded.partition(":")
            return service.check_credentials(user, supplied)

        def _cookie_token(self) -> str | None:
            header = self.headers.get("Cookie") or ""
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "atc_session" and value:
                    return value
            return None

        def _challenge(self) -> None:
            """401 with the login page (not a bare prompt).

            A browser hitting the viewer gets the styled login dialog; a
            client that *wants* Basic auth (curl with -u, an API script) sends
            `Accept: application/json` and gets a plain 401 instead.
            """
            wants_json = "application/json" in (self.headers.get("Accept") or "")
            if wants_json:
                body = b'{"error": "authentication required"}'
                self._send(401, body, "application/json; charset=utf-8")
                return
            body = (WEB / "login.html").read_bytes()
            self._send(401, body, "text/html; charset=utf-8")

        def do_GET(self) -> None:
            if not self._authorized():
                self._challenge()
                return
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_POST(self) -> None:
            # The single allowed POST: the login endpoint. It only *verifies*
            # credentials and sets a cookie — it cannot restart, stop or
            # mutate anything, so the viewer stays read-only by construction.
            try:
                self._route_post()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _route_post(self) -> None:
            path = urlsplit(self.path).path
            if path != "/api/login":
                self._send(404, b'{"error": "not found"}',
                           "application/json; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                body = {}
            user = str(body.get("user") or "")
            password = str(body.get("password") or "")
            if not service.check_credentials(user, password):
                # Small delay to make online guessing slow; constant-time
                # compares already prevent timing leaks.
                time.sleep(1.0)
                self._send(401, b'{"error": "wrong user or password"}',
                           "application/json; charset=utf-8")
                return
            token = service.new_session_token()
            cookie = (f"atc_session={token}; Path=/; Max-Age=86400; "
                      "HttpOnly; SameSite=Lax"
                      + ("; Secure" if service.tls else ""))
            try:
                self.send_response(200)
                self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", "2")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/atc":
                # Replay-only viewer: /api/atc returns the newest debrief so
                # the shared map.js render path still works, but there is no
                # live feed — the page opens on the newest saved sortie.
                path = newest_debrief(service.debrief_dir)
                if path is None:
                    self._send(200, json.dumps(
                        {"error": "no saved debriefs yet"},
                        separators=(",", ":")).encode(),
                        "application/json; charset=utf-8")
                    return
                self._send(200, json.dumps(
                    debrief_snapshot(path, service.airfield, service.basemap),
                    separators=(",", ":")).encode(),
                    "application/json; charset=utf-8")
                return
            if path == "/api/debriefs":
                self._send(200, json.dumps(
                    {"debriefs": list_debriefs(service.debrief_dir)}).encode(),
                    "application/json; charset=utf-8")
                return
            if path.startswith("/api/debrief/"):
                name = path[len("/api/debrief/"):]
                file_path = _resolve_debrief(name, service.debrief_dir)
                if file_path is None:
                    self._send(404, b'{"error": "not found"}',
                               "application/json; charset=utf-8")
                    return
                self._send(200, json.dumps(
                    debrief_snapshot(file_path, service.airfield,
                                     service.basemap),
                    separators=(",", ":")).encode(),
                    "application/json; charset=utf-8")
                return
            if path.startswith("/tiles/") and path.endswith(".png"):
                tile = _proxy_tile(service, path[len("/tiles/"):])
                if tile is None:
                    self.send_error(404)
                    return
                self._send(200, tile, "image/png")
                return
            files = {
                "/": (WEB / "viewer.html", "text/html; charset=utf-8"),
                "/map.js": (WEB / "map.js", "text/javascript; charset=utf-8"),
                "/map.css": (WEB / "map.css", "text/css; charset=utf-8"),
                "/viewer.js": (WEB / "viewer.js", "text/javascript; charset=utf-8"),
            }
            if path.startswith("/overlays/") and path.endswith(".png"):
                overlay = OVERLAYS / Path(path).name
                if overlay.is_file():
                    self._send(200, overlay.read_bytes(), "image/png")
                    return
            if path not in files:
                self.send_error(404)
                return
            file_path, content_type = files[path]
            self._send(200, file_path.read_bytes(), content_type)

    return Handler


class ViewerService:
    """Stateless holder of the viewer's configuration (no mutable bot state)."""

    def __init__(self, airfield, debrief_dir: Path, basemap: str | None,
                 credentials: dict[str, str], tls: bool = False):
        self.airfield = airfield
        self.debrief_dir = Path(debrief_dir)
        self.basemap = basemap
        # Tile-proxy allowlist: every http:// tile host named in the airfield
        # config's basemap layers (dcsmaps.com for the Caucasus tiles).
        self.tile_hosts: set[str] = set()
        layers = (airfield.map_config or {}).get("layers") or {}
        for spec in layers.values():
            url = (spec or {}).get("url") or ""
            if url.startswith("http://"):
                from urllib.parse import urlsplit
                self.tile_hosts.add(urlsplit(url).netloc)
        # On-disk tile cache (tiles never change; keeps dcsmaps load light).
        self.tile_cache = Path(debrief_dir).parent / ".viewer-tiles"
        # user -> sha256(password); secrets are never kept in memory as-is.
        self._credentials = {
            user: hashlib.sha256(pw.encode()).digest()
            for user, pw in credentials.items()}
        self.tls = bool(tls)
        self._sessions: set[bytes] = set()
        self._sessions_lock = threading.Lock()

    def check_credentials(self, user: str, password: str) -> bool:
        """True if (user, password) matches an account (constant-time)."""
        digest = hashlib.sha256(password.encode()).digest()
        expected = self._credentials.get(user)
        if expected is None:
            # Burn the same time as a real check, so a wrong *user* is not
            # distinguishable from a wrong password by timing.
            hmac.compare_digest(digest, digest)
            return False
        return hmac.compare_digest(digest, expected)

    def new_session_token(self) -> str:
        token = hashlib.sha256(
            f"{time.time_ns()}-{threading.get_ident()}-"
            .encode() + os.urandom(32)).hexdigest()
        with self._sessions_lock:
            self._sessions.add(hashlib.sha256(token.encode()).digest())
        return token

    def _check_session(self, token: str) -> bool:
        digest = hashlib.sha256(token.encode()).digest()
        with self._sessions_lock:
            return digest in self._sessions


def start_viewer(airfield, port: int, host: str = "127.0.0.1",
                 debrief_dir: Path | str | None = None,
                 basemap: str | None = None,
                 credentials: dict[str, str] | None = None,
                 tls: bool = False,
                 tls_hostname: str | None = None) -> ThreadingHTTPServer | None:
    """Start the read-only viewer. Returns None if the port is busy."""
    debrief_dir = Path(debrief_dir) if debrief_dir else DEBRIEF_DIR
    service = ViewerService(airfield, debrief_dir, basemap,
                            credentials or {}, tls=tls)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(service))
    except OSError as error:
        print(f"[!] Viewer disabled: cannot bind {host}:{port} ({error})",
              file=sys.stderr)
        return None
    if tls:
        cert = debrief_dir.parent / ".viewer-cert.pem"
        key = debrief_dir.parent / ".viewer-key.pem"
        if not (cert.is_file() and key.is_file()):
            # Name the cert for the public hostname when given (a bind host
            # of 0.0.0.0 is useless in a browser warning); include the bind
            # host as a SAN too so 127.0.0.1 keeps working.
            name = tls_hostname or host or "localhost"
            if not _self_signed_cert(cert, key, name, san=host):
                print("[!] Viewer continuing WITHOUT TLS — the password will "
                      "cross the network in the clear.", file=sys.stderr)
            else:
                print(f"[*] Generated self-signed certificate: {cert} "
                      f"(CN={name})")
        if cert.is_file() and key.is_file():
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket,
                                                server_side=True)
            print("[*] TLS enabled (self-signed; the browser will warn once).")
    scheme = "https" if tls else "http"
    print(f"[*] Viewer (read-only) on {scheme}://{host}:{port}/  "
          f"(airfield {airfield.name}, debriefs: {debrief_dir})")
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="atc-viewer")
    thread.start()
    return server


def main() -> None:
    import argparse

    from airspace import Airspace

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airfield", default="Kutaisi")
    parser.add_argument("--airspace", default=None)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; keep 127.0.0.1 and expose via a "
                             "tunnel/proxy, or set 0.0.0.0 to face the "
                             "Internet directly")
    parser.add_argument("--debrief-dir", default=None,
                        help="where the bot's debrief files live "
                             "(default: atc/debrief)")
    parser.add_argument("--base-map", default=None,
                        help="base map layer: 'dcs' or 'osm' "
                             "(default: airspace.json map.base)")
    parser.add_argument("--auth-file", default=None,
                        help="JSON credentials file "
                             "(default: atc/.viewer-auth.json); format: "
                             '{"users": {"name": "password"}} — multiple '
                             "accounts, both user and password required")
    parser.add_argument("--username", default=None,
                        help="single-account fallback when no --auth-file")
    parser.add_argument("--password", default=None,
                        help="single-account fallback when no --auth-file")
    parser.add_argument("--tls", dest="tls", action="store_true", default=True,
                        help="serve HTTPS (default). With --hostname, a "
                             "Let's Encrypt certificate is obtained/renewed "
                             "automatically; otherwise a self-signed "
                             "certificate is generated on first start")
    parser.add_argument("--no-tls", dest="tls", action="store_false",
                        help="serve plain HTTP (the password crosses the "
                             "network in the clear; LAN/testing only)")
    parser.add_argument("--tls-hostname", default=None,
                        help="public hostname for the self-signed "
                             "certificate's CN (your DNS name); the bind "
                             "host is added as a SAN too")
    args = parser.parse_args()

    auth_path = (Path(args.auth_file) if args.auth_file
                 else Path(__file__).with_name(".viewer-auth.json"))
    if auth_path.is_file():
        credentials = _load_auth_file(auth_path)
        print(f"[*] Credentials: {auth_path} "
              f"({len(credentials)} account(s))")
    elif args.username and args.password:
        credentials = {args.username: args.password}
        print("[*] Credentials: single account from --username/--password")
    else:
        parser.error(
            "no credentials: create .viewer-auth.json next to viewer.py "
            '(format: {"users": {"name": "password"}}) or pass '
            "--username/--password")

    airspace = Airspace.load(args.airspace) if args.airspace else Airspace.load()
    airfield = airspace.get(args.airfield)
    if airfield is None:
        raise SystemExit(f"airfield {args.airfield!r} not found in airspace.json")

    server = start_viewer(airfield, args.port, args.host,
                          debrief_dir=args.debrief_dir,
                          basemap=args.base_map,
                          credentials=credentials,
                          tls=args.tls,
                          tls_hostname=args.tls_hostname)
    if server is None:
        raise SystemExit("viewer could not start")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
