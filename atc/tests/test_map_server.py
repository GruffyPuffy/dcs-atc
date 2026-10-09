"""Map view: airspace payload, live aircraft with phase, HTTP endpoints."""

import json
import threading
import urllib.request

from brain import Controller
from map_server import MapService, TrackHistory, make_handler, start_map_server
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


def test_chatter_atc_lines_resolve_callsign(brain, airfield):
    # an ATC reply opens with the callsign, so the log can tag it to the flight
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("tx", 250.0, "ATC", "Colt 1, Ground.", "ground")
    assert service.chatter()[-1]["callsign"] == "Colt 1"


# ---------- Flight-path trails ----------

def test_track_history_records_path():
    t = TrackHistory()
    t.update("Colt 1", 42.18, 42.50, distance_nm=5.0, alt_ft=1000)
    t.update("Colt 1", 42.19, 42.51, distance_nm=5.0, alt_ft=1200)
    assert t.path("Colt 1") == [[42.18, 42.50, 1000], [42.19, 42.51, 1200]]


def test_track_history_stops_beyond_radius():
    t = TrackHistory(radius_nm=40.0)
    t.update("Colt 1", 42.18, 42.50, distance_nm=5.0)
    t.update("Colt 1", 43.00, 43.00, distance_nm=80.0)  # off-station
    assert len(t.path("Colt 1")) == 1


def test_track_history_skips_tiny_moves():
    t = TrackHistory(min_move_nm=0.02)
    t.update("Colt 1", 42.1800, 42.5000, distance_nm=5.0)
    t.update("Colt 1", 42.1800, 42.5000, distance_nm=5.0)  # identical
    assert len(t.path("Colt 1")) == 1


def test_track_history_caps_points():
    t = TrackHistory(max_points=3)
    for i in range(10):
        t.update("Colt 1", 42.18 + i * 0.01, 42.50, distance_nm=5.0)
    assert len(t.path("Colt 1")) == 3


def test_track_history_prunes_absent():
    t = TrackHistory()
    t.update("Colt 1", 42.18, 42.50, distance_nm=5.0)
    t.update("Ford 2", 42.19, 42.51, distance_nm=5.0)
    t.prune({"Colt 1"})
    assert t.path("Ford 2") == []
    assert len(t.path("Colt 1")) == 1


def test_snapshot_includes_path(brain, airfield):
    # two snapshots at different positions -> the aircraft carries a trail
    state = FakeState([_ac(lat=42.18, lon=42.50)])
    service = MapService(airfield, brain, state)
    service.snapshot()
    state._aircraft = [_ac(lat=42.20, lon=42.52)]
    snap = service.snapshot()
    path = snap["aircraft"][0]["path"]
    assert len(path) == 2
    assert path[0][:2] == [42.18, 42.50]
    assert path[0][2] == 1200  # altitude carried on the point


def test_phase_transition_recorded_on_path(brain, airfield):
    # a phase change shows up as a 'state' event on the timeline
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    service.snapshot()
    states = [c for c in service.tracks.comms("Colt 1") if c["kind"] == "state"]
    assert any(c["text"] == "Taxi" for c in states)


def test_phase_transition_only_on_change(brain, airfield):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    service.snapshot()
    service.snapshot()  # same phase -> no new state event
    states = [c for c in service.tracks.comms("Colt 1") if c["kind"] == "state"]
    assert len(states) == 1


# ---------- Comm markers on the path ----------

def test_comm_marker_attached_to_path(brain, airfield):
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    service.snapshot()  # records the position
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1, requesting taxi",
                        "ground")
    comms = service.tracks.comms("Colt 1")
    assert len(comms) == 1
    assert comms[0]["kind"] == "rx"
    assert comms[0]["text"] == "Ground, Colt 1, requesting taxi"
    assert comms[0]["lat"] == 42.18  # pinned to the last known position


def test_atc_reply_pinned_to_pilot_position(brain, airfield):
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    service.snapshot()
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1", "ground")
    service.log_chatter("tx", 250.0, "ATC", "Colt 1, Ground.", "ground")
    comms = service.tracks.comms("Colt 1")
    assert [c["kind"] for c in comms] == ["rx", "tx"]


def test_comm_marker_skipped_without_position(brain, airfield):
    # a call before any position is known has nowhere to pin -> no marker
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1", "ground")
    assert service.tracks.comms("Colt 1") == []


def test_snapshot_includes_comms(brain, airfield):
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    service.snapshot()
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1", "ground")
    snap = service.snapshot()
    assert len(snap["aircraft"][0]["comms"]) == 1


def test_all_comms_pinned_to_path(brain, airfield):
    # every kind of comm — pilot call, ATC reply, automatic call — is pinned to
    # the aircraft's path, so the timeline shows ALL comms.
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([_ac()]))
    service.snapshot()
    service.log_chatter("rx", 263.0, "Caveman", "Tower, Colt 1, on final", "tower")
    service.log_chatter("tx", 263.0, "ATC", "Colt 1, Tower, cleared to land.", "tower")
    # an automatic call (go-around) — also a tx opening with the callsign
    service.log_chatter("tx", 263.0, "ATC",
                        "Colt 1, Tower, go around, runway 25 is occupied.", "tower")
    comms = service.tracks.comms("Colt 1")
    assert [c["kind"] for c in comms] == ["rx", "tx", "tx"]
    assert "go around" in comms[-1]["text"]
    assert all(c["lat"] == 42.18 for c in comms)  # all pinned to the position


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
