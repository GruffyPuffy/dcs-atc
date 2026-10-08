"""Map view: airspace payload, live aircraft with phase, HTTP endpoints."""

import json
import threading
import urllib.request

from brain import Controller
from map_server import MapService, make_handler, start_map_server
from state_client import Aircraft
from http.server import ThreadingHTTPServer


class FakeState:
    """Stand-in for StateClient: returns a fixed list of player aircraft."""

    def __init__(self, aircraft, ai_air=None):
        self._aircraft = aircraft
        self._ai_air = ai_air or []

    def aircraft(self):
        return list(self._aircraft)

    def all_units(self):
        return list(self._aircraft) + list(self._ai_air)


def _ac(callsign="Colt 1", player="Caveman", lat=42.18, lon=42.50):
    return Aircraft(callsign=callsign, player=player, type="F/A-18C",
                    lat=lat, lon=lon, alt_ft=1200.0, heading=250.0, coalition=2)


def test_snapshot_has_airspace_geometry(brain, airfield):
    service = MapService(airfield, brain, FakeState([]))
    snap = service.snapshot()
    af = snap["airfield"]
    assert af["name"] == "Kutaisi"
    assert len(af["ctr"]["polygon"]) >= 3          # CTR outline for the map
    assert set(af["gates"]) == {"East", "West", "South", "North"}
    assert "25" in af["runways"]
    assert snap["aircraft"] == []


def test_overlay_payload_when_generated(brain, airfield):
    # the georeferenced chart overlay ships with the repo
    snap = MapService(airfield, brain, FakeState([])).snapshot()
    overlay = snap["airfield"]["overlay"]
    assert overlay is not None
    assert overlay["url"] == "/overlays/kutaisi.png"
    (south, west), (north, east) = overlay["bounds"]
    assert south < north and west < east
    # bounds should straddle the airfield
    assert south < airfield.ctr.center_lat < north
    assert west < airfield.ctr.center_lon < east


def test_snapshot_reports_aircraft_phase(brain, airfield):
    # a pilot who has talked to Ground should show phase=Taxi on the map
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    snap = service.snapshot()
    assert len(snap["aircraft"]) == 1
    ac = snap["aircraft"][0]
    assert ac["callsign"] == "Colt 1"
    assert ac["phase"] == "Taxi"
    assert ac["controller"] == "ground"
    assert ac["alt_ft"] == 1200


def test_snapshot_survives_state_bridge_error(brain, airfield):
    class Broken:
        def aircraft(self):
            raise RuntimeError("bridge down")

    snap = MapService(airfield, brain, Broken()).snapshot()
    assert snap["aircraft"] == []
    assert "bridge down" in snap["error"]


def test_chatter_resolves_pilot_callsign(brain, airfield):
    # once the brain has learned the SRS name -> callsign, the chatter log
    # should carry the flight callsign so the map can show "Caveman (Colt 1)".
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1, requesting taxi",
                        "ground")
    entry = service.chatter()[-1]
    assert entry["who"] == "Caveman"
    assert entry["callsign"] == "Colt 1"


def test_chatter_callsign_empty_before_learned(brain, airfield):
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1", "ground")
    assert service.chatter()[-1]["callsign"] == ""


def test_chatter_atc_lines_have_no_callsign(brain, airfield):
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("tx", 250.0, "ATC", "Colt 1, Ground.", "ground")
    assert service.chatter()[-1]["callsign"] == ""


def test_http_endpoints_serve_api_and_page(brain, airfield):
    service = MapService(airfield, brain, FakeState([_ac()]))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/atc"))
        assert data["airfield"]["name"] == "Kutaisi"
        assert len(data["aircraft"]) == 1
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read()
        assert b'id="map"' in html
        js = urllib.request.urlopen(f"http://127.0.0.1:{port}/map.js").read()
        assert b"L.map" in js
    finally:
        server.shutdown()


def test_start_map_server_returns_running_server(brain, airfield):
    server, service = start_map_server(airfield, brain, FakeState([]), 0,
                                       host="127.0.0.1", log=lambda _line: None)
    try:
        assert server.server_address[1] > 0
        assert service is not None
    finally:
        server.shutdown()


def test_start_map_server_survives_port_in_use(brain, airfield):
    # a busy port must not take the bot down: returns None and logs a warning
    first, _ = start_map_server(airfield, brain, FakeState([]), 0,
                                host="127.0.0.1", log=lambda _line: None)
    port = first.server_address[1]
    messages = []
    try:
        second, _service = start_map_server(airfield, brain, FakeState([]), port,
                                            host="127.0.0.1", log=messages.append)
        assert second is None
        assert any("disabled" in m for m in messages)
    finally:
        first.shutdown()


def test_chatter_log_records_and_returns(brain, airfield):
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1, requesting taxi",
                        "ground")
    service.log_chatter("tx", 250.0, "ground", "Colt 1, Ground, cleared taxi...",
                        "ground")
    service.log_chatter("tx", 270.5, "atis", "Kutaisi information Oscar...", "atis")
    log = service.chatter()
    assert len(log) == 3
    assert log[0]["kind"] == "rx" and log[0]["who"] == "Caveman"
    assert log[2]["controller"] == "atis"
    # chatter is included in the snapshot the page polls
    assert len(service.snapshot()["chatter"]) == 3
