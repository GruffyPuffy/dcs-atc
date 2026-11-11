#!/usr/bin/env python3
"""Standalone read-only DCS mission observer."""
import argparse
import json
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent


class Observer:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.tracks = defaultdict(deque)
        self.snapshot = {"aircraft": [], "bullseyes": {}, "error": "Connecting to DCS…", "srs": "Not connected"}
        self.running = True

    def exchange(self, op, **fields):
        req = {"v": 1, "id": uuid.uuid4().hex, "op": op, **fields}
        with socket.create_connection((self.args.dcs_host, self.args.dcs_port), timeout=4) as conn:
            conn.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode())
            with conn.makefile("rb") as stream:
                reply = json.loads(stream.readline(2 * 1024 * 1024))
        if reply.get("id") != req["id"] or not reply.get("ok"):
            raise RuntimeError(reply.get("error", "Invalid bridge reply"))
        return reply["result"]

    def poll(self):
        interval = 1 / max(0.2, self.args.rate)
        while self.running:
            now = time.time()
            try:
                data = self.exchange("status")
                aircraft = []
                seen = set()
                for group in data.get("groups", []):
                    for unit in group.get("units", []):
                        player = unit.get("player", "") or ""
                        group_name = group.get("name", "")
                        ident = f"{group_name}/{unit.get('name', 'Unknown')}"
                        # Newer bridge payloads may include the DCS
                        # Unit:isActive() result. Treat only explicit false as
                        # inactive so this remains compatible with the current
                        # bridge, which doesn't send the field yet.
                        if unit.get("active") is False and not player:
                            self.tracks.pop(ident, None)
                            continue
                        # Unoccupied DCS dynamic aircraft slots appear in the
                        # bridge snapshot like ordinary units. Hide those for
                        # now; once occupied, getPlayerName() identifies them
                        # as a real player unit and they should be shown.
                        slot_label = (
                            f"{group_name} {unit.get('name', '')} "
                            f"{unit.get('type', '')}"
                        ).lower()
                        slot_name = "".join(c for c in slot_label if c.isalnum())
                        if (group.get("category") == 0 and not player
                                and "dynamicslot" in slot_name):
                            self.tracks.pop(ident, None)
                            continue
                        lat, lon = unit.get("lat"), unit.get("lon")
                        if lat is None or lon is None:
                            continue
                        name = unit.get("name", "Unknown")
                        seen.add(ident)
                        ac = {"id": ident, "name": name, "group": group_name,
                              "player": player, "type": unit.get("type", ""),
                              "coalition": group.get("coalition", 0), "category": group.get("category", 0),
                              "lat": float(lat), "lon": float(lon), "alt": float(unit.get("alt", 0)) * 3.280839895,
                              "heading": float(unit.get("heading", 0))}
                        aircraft.append(ac)
                        trail = self.tracks[ident]
                        trail.append([now, ac["lat"], ac["lon"]])
                cutoff = now - self.args.history * 60
                for key, trail in list(self.tracks.items()):
                    while trail and trail[0][0] < cutoff:
                        trail.popleft()
                    if not trail and key not in seen:
                        del self.tracks[key]
                for ac in aircraft:
                    ac["path"] = [[p[0], p[1], p[2]] for p in self.tracks[ac["id"]]]
                bullseyes = {}
                for coalition in (2, 1):
                    try:
                        point = self.exchange("bullseye", coalition=coalition)
                    except Exception:
                        point = None
                    if point:
                        bullseyes[str(coalition)] = [point["lat"], point["lon"]]
                with self.lock:
                    self.snapshot = {"aircraft": aircraft,
                                     "bullseyes": bullseyes,
                                     "error": None, "updated": now,
                                     "history_minutes": self.args.history,
                                     "srs": self.snapshot.get("srs", "Not connected")}
            except Exception as exc:
                with self.lock:
                    self.snapshot["error"] = f"DCS bridge: {exc}"
            time.sleep(interval)

    def monitor_srs(self):
        # Establish an SRS TCP session for this prototype. Radio selection and
        # audio reception will be layered on after the map/tracking milestone.
        while self.running:
            try:
                with socket.create_connection((self.args.srs_host, self.args.srs_port), timeout=5) as conn:
                    conn.settimeout(1)
                    guid = uuid.uuid4().hex[:22]
                    client = {"ClientGuid": guid, "Name": self.args.name, "Seat": 0,
                              "Coalition": 0, "RadioInfo": {"radios": [{"enc": False, "encKey": 0,
                              "freq": 1.0, "modulation": 3, "secFreq": 0.0, "retransmit": False}
                              for _ in range(11)], "unit": "Observer",
                              "unitId": 100000, "iff": {"control": 0, "mode1": 0, "mode3": 0,
                              "mode4": False, "mic": -1, "status": 0}, "ambient": {"vol": 0.0, "abType": ""}},
                              "LatLngPosition": {"lat": 0, "lng": 0, "alt": 0}}
                    conn.sendall((json.dumps({"Client": client, "MsgType": 0, "Version": "2.4.1.0"}, separators=(",", ":"))+"\n").encode())
                    with self.lock:
                        self.snapshot["srs"] = "TCP connected; waiting for sync"
                    buffer = b""
                    while self.running:
                        try:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            buffer += chunk
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                try: msg = json.loads(line)
                                except json.JSONDecodeError: continue
                                if msg.get("MsgType") == 2:
                                    if self.args.eam:
                                        auth = {"Client": client, "MsgType": 7, "Version": "2.4.1.0",
                                                "ExternalAWACSModePassword": self.args.eam}
                                        conn.sendall((json.dumps(auth, separators=(",", ":"))+"\n").encode())
                                    update = {"Client": client, "MsgType": 3, "Version": "2.4.1.0"}
                                    conn.sendall((json.dumps(update, separators=(",", ":"))+"\n").encode())
                                    with self.lock:
                                        self.snapshot["srs"] = "Logged in; radio monitoring comes next"
                        except socket.timeout:
                            continue
            except Exception as exc:
                with self.lock:
                    self.snapshot["srs"] = f"Disconnected: {exc}"
            time.sleep(3)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dcs-host", default="127.0.0.1", help="DCS bridge host/IP")
    p.add_argument("--dcs-port", type=int, default=10309)
    p.add_argument("--srs-host", default="127.0.0.1", help="SRS server host/IP")
    p.add_argument("--srs-port", type=int, default=5002)
    p.add_argument("--name", default="Observer")
    p.add_argument("--eam", default=None, help="optional SRS External AWACS Mode password")
    p.add_argument("--web-port", type=int, default=8091)
    p.add_argument("--history", type=int, default=180, help="path history in minutes (max 180)")
    p.add_argument("--rate", type=float, default=1.0, help="DCS polling samples per second")
    args = p.parse_args()
    args.history = max(1, min(180, args.history))
    app = Observer(args)
    threading.Thread(target=app.poll, daemon=True).start()
    threading.Thread(target=app.monitor_srs, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                with app.lock:
                    body = json.dumps(app.snapshot).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            else:
                target = ROOT / ("index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/"))
                if not target.is_file() or target.parent != ROOT:
                    self.send_error(404); return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8" if target.suffix == ".html" else "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass

    server = ThreadingHTTPServer(("0.0.0.0", args.web_port), Handler)
    print(f"Observer map: http://localhost:{args.web_port}")
    print(f"DCS bridge: {args.dcs_host}:{args.dcs_port} | SRS: {args.srs_host}:{args.srs_port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: app.running = False; server.server_close()


if __name__ == "__main__": main()
