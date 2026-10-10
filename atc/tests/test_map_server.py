"""Map view: airspace payload, live aircraft with phase, HTTP endpoints."""

import json
import threading
import urllib.error
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


def test_record_captures_trail_without_a_browser(brain, airfield, tmp_path):
    """`record` must save trails even when nothing polls the map page."""
    tracks_file = tmp_path / "tracks.json"
    service = MapService(airfield, brain, FakeState([]),
                         tracks_file=tracks_file)
    service.record([_ac(lat=42.18, lon=42.50)])
    service.record([_ac(lat=42.20, lon=42.52)])  # moved enough to be kept
    service.save_tracks()
    saved = json.loads(tracks_file.read_text())
    # Until a transmission is heard the trail is keyed by the DCS/SRS name.
    assert "Caveman" in saved["tracks"]
    assert len(saved["tracks"]["Caveman"]) >= 2


def test_record_marks_active_and_retains_after_leaving(brain, airfield, tmp_path):
    tracks_file = tmp_path / "tracks.json"
    service = MapService(airfield, brain, FakeState([]),
                         tracks_file=tracks_file)
    service.record([_ac()])
    assert service.tracks.is_active("Caveman")
    # Aircraft leaves the mission: trail is retained (not pruned).
    service.record([])
    assert not service.tracks.is_active("Caveman")
    assert "Caveman" in service.tracks.callsigns()


def test_debrief_filename_is_timestamped_and_slugged():
    from map_server import debrief_filename
    name = debrief_filename("Gudauta", when=0)  # epoch = deterministic-ish
    assert name.startswith("tracks_gudauta_") and name.endswith(".json")


def test_replay_loads_and_freezes(brain, airfield, tmp_path):
    """A tracks_file that already exists is loaded read-only (a replay)."""
    tracks_file = tmp_path / "tracks.json"
    tracks_file.write_text(json.dumps({
        "tracks": {"Caveman": [[42.18, 42.50, 1200.0], [42.20, 42.52, 1100.0]]},
        "comms": {"Caveman": [{"kind": "rx", "text": "hi", "lat": 42.18,
                               "lon": 42.50}]},
    }))
    service = MapService(airfield, brain, FakeState([]), tracks_file=tracks_file)
    assert service.replay is True
    # record() must not mutate a saved replay.
    service.record([_ac(lat=42.9, lon=42.9)])
    assert len(service.tracks.path("Caveman")) == 2
    # Snapshot shows the retained trail even with no live state and no data.
    snap = service.snapshot()
    assert snap["replay"] is True
    assert any(a["callsign"] == "Caveman" and a["active"] is False
               and len(a["path"]) == 2 for a in snap["aircraft"])


def test_snapshot_retained_trail_without_live_state(brain, airfield):
    """A MapService with no state still renders a retained trail."""
    service = MapService(airfield, brain, None)
    service.tracks.update("Caveman", 42.18, 42.50, 2.0, 1200.0)
    snap = service.snapshot()
    assert any(a["callsign"] == "Caveman" for a in snap["aircraft"])


def test_basemap_defaults_to_dcs_config(brain, airfield):
    # airspace.json sets map.base = "dcs"; the payload carries the tile options
    snap = MapService(airfield, brain, FakeState([])).snapshot()
    bm = snap["airfield"]["basemap"]
    assert bm["name"] == "dcs"
    assert "dcsmaps.com" in bm["url"]
    assert bm["tms"] is True
    assert bm["maxNativeZoom"] == 12


def test_basemap_choice_override(brain, airfield):
    snap = MapService(airfield, brain, FakeState([]), basemap="osm").snapshot()
    bm = snap["airfield"]["basemap"]
    assert bm["name"] == "osm"
    assert "openstreetmap" in bm["url"]
    assert bm["tms"] is False


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


def test_track_history_retains_absent():
    # Trails are NOT dropped when an aircraft leaves: a completed sortie must
    # stay reviewable for debrief (the bug this guards against).
    t = TrackHistory()
    t.update("Colt 1", 42.18, 42.50, distance_nm=5.0)
    t.update("Ford 2", 42.19, 42.51, distance_nm=5.0)
    t.set_active({"Colt 1"})
    assert len(t.path("Ford 2")) == 1          # retained, not pruned
    assert t.is_active("Colt 1")
    assert not t.is_active("Ford 2")
    assert sorted(t.callsigns()) == ["Colt 1", "Ford 2"]


def test_track_history_save_load(tmp_path):
    t = TrackHistory()
    t.update("Colt 1", 42.18, 42.50, distance_nm=5.0, alt_ft=1000)
    t.add_comm("Colt 1", 42.18, 42.50, "rx", "hello", "ground", "12:00:00")
    path = tmp_path / "tracks.json"
    t.save(path)
    restored = TrackHistory()
    restored.load(path)
    assert restored.path("Colt 1") == [[42.18, 42.50, 1000]]
    assert restored.comms("Colt 1")[0]["text"] == "hello"


def test_snapshot_retains_departed_aircraft(brain, airfield):
    # A pilot who logs off must still leave a (dimmed) trail for review.
    state = FakeState([_ac()])
    service = MapService(airfield, brain, state)
    service.snapshot()
    state._aircraft = []                      # pilot leaves the slot
    snap = service.snapshot()
    assert len(snap["aircraft"]) == 1         # retained, not pruned
    gone = snap["aircraft"][0]
    assert gone["active"] is False
    assert len(gone["path"]) >= 1
    assert [a for a in snap["aircraft"] if a["active"]] == []


def test_map_service_persists_tracks(brain, airfield, tmp_path):
    path = tmp_path / "tracks.json"
    MapService(airfield, brain, FakeState([_ac()]),
               tracks_file=path).snapshot()
    assert path.exists()                      # autosaved on snapshot
    # A fresh service (e.g. after a restart) restores the trail.
    snap = MapService(airfield, brain, FakeState([]),
                      tracks_file=path).snapshot()
    assert any(a["path"] for a in snap["aircraft"])


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


def test_list_load_unload_debrief(brain, airfield, tmp_path, monkeypatch):
    """The web loader: list saved files, load one read-only, then resume live."""
    import map_server
    monkeypatch.setattr(map_server, "DEBRIEF_DIR", tmp_path)
    (tmp_path / "tracks_gudauta_test.json").write_text(json.dumps({
        "tracks": {"Springfield 1": [[43.10, 40.57, 69.0],
                                     [43.12, 40.56, 800.0]]},
        "comms": {"Springfield 1": [{"kind": "rx", "text": "hi",
                                     "lat": 43.10, "lon": 40.57}]},
    }))
    service = MapService(airfield, brain, None)

    listed = service.list_debriefs()
    assert any(d["name"] == "tracks_gudauta_test.json" for d in listed)

    assert service.load_debrief("tracks_gudauta_test.json") == {
        "ok": True, "name": "tracks_gudauta_test.json"}
    assert service.replay is True
    assert service.snapshot()["replay_name"] == "tracks_gudauta_test.json"

    # Path traversal / bad names are refused and change nothing.
    assert service.load_debrief("../secrets.json")["ok"] is False
    assert service.load_debrief("nope.json")["ok"] is False

    # Unload returns to live mode and clears the trail.
    assert service.unload_debrief()["ok"] is True
    assert service.replay is False
    assert service.snapshot()["aircraft"] == []


def test_load_rejects_empty_debrief(brain, airfield, tmp_path, monkeypatch):
    """Loading an empty debrief fails clearly instead of showing a blank map."""
    import map_server
    monkeypatch.setattr(map_server, "DEBRIEF_DIR", tmp_path)
    (tmp_path / "tracks_empty.json").write_text('{"tracks": {}, "comms": {}}')
    service = MapService(airfield, brain, None)
    result = service.load_debrief("tracks_empty.json")
    assert result["ok"] is False and "empty" in result["error"].lower()
    assert service.replay is False  # nothing changed


def test_save_skips_empty_history(brain, airfield, tmp_path):
    """An empty history must not leave an (unusable) debrief file behind."""
    tracks_file = tmp_path / "tracks.json"
    service = MapService(airfield, brain, FakeState([]), tracks_file=tracks_file)
    service.save_tracks()               # nothing recorded yet
    assert not tracks_file.exists()
    service.record([_ac(lat=42.18, lon=42.50)])
    service.record([_ac(lat=42.20, lon=42.52)])
    service.save_tracks()               # now there is a trail
    assert tracks_file.exists()


def test_unrecognized_pilot_call_still_on_trail(brain, airfield, tmp_path):
    """Every spoken call is on the trail, even if no callsign was understood."""
    service = MapService(airfield, brain, None)
    # A trail exists keyed by the raw speaker name (not yet learned to a
    # callsign): "Caveman".
    service.tracks.update("Caveman", 42.18, 42.50, 2.0, 1200.0)
    # A pilot call with no parseable callsign -> attached under the raw name.
    service.log_chatter("rx", 250.0, "Caveman", "uhh ground mumble")
    comms = service.tracks.comms("Caveman")
    assert any(c["kind"] == "rx" and "mumble" in c["text"] for c in comms)


def test_debrief_round_trips_chatter(brain, airfield, tmp_path):
    """The radio log is saved with the debrief and restored on replay."""
    tracks_file = tmp_path / "tracks.json"
    service = MapService(airfield, brain, FakeState([]), tracks_file=tracks_file)
    service.record([_ac(lat=42.18, lon=42.50)])
    service.record([_ac(lat=42.20, lon=42.52)])
    service.log_chatter("tx", 263.0, "Caveman", "Colt 1, cleared taxi", "tower")
    service.save_tracks()

    restored = MapService(airfield, brain, None, tracks_file=tracks_file)
    assert restored.replay is True
    assert any("cleared taxi" in e["text"] for e in restored.chatter())


def test_replay_ignores_live_chatter(brain, airfield, tmp_path):
    tracks_file = tmp_path / "tracks.json"
    (tracks_file).write_text(json.dumps(
        {"tracks": {"Colt 1": [[42.18, 42.50, 100.0], [42.19, 42.51, 200.0]]},
         "comms": {}, "chatter": [{"kind": "tx", "text": "old", "who": "",
                                   "callsign": "", "controller": "tower",
                                   "freq": 263.0, "t": "10:00:00"}]}))
    service = MapService(airfield, brain, None, tracks_file=tracks_file)
    assert service.replay is True
    before = len(service.chatter())
    service.log_chatter("rx", 263.0, "Caveman", "live call")
    assert len(service.chatter()) == before  # replay keeps only the saved log


def test_debrief_endpoints_http(brain, airfield, tmp_path, monkeypatch):
    import map_server
    monkeypatch.setattr(map_server, "DEBRIEF_DIR", tmp_path)
    (tmp_path / "tracks_k_test.json").write_text(json.dumps(
        {"tracks": {"Colt 1": [[42.18, 42.50, 100.0]]}, "comms": {}}))
    service = MapService(airfield, brain, None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        listing = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/debriefs"))
        assert listing["debriefs"][0]["name"] == "tracks_k_test.json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/debrief/load",
            data=json.dumps({"name": "tracks_k_test.json"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        # A bad name is a 400.
        bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/debrief/load",
            data=json.dumps({"name": "../x.json"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(bad)
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        server.shutdown()


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


# ---------- Web-driven restart / airfield switch ----------

def test_control_plane_validates_allowlist(tmp_path):
    from map_server import ControlPlane
    state_file = tmp_path / ".control"
    calls = []
    cp = ControlPlane(["Kutaisi", "Gudauta"], state_file,
                      lambda *a: calls.append(a), "Kutaisi")
    # An unknown airfield is refused and writes nothing.
    result = cp.restart("Batumi", save_debrief=False)
    assert result["ok"] is False
    assert not state_file.exists()
    # A known airfield is accepted and written as plain key=value.
    result = cp.restart("Gudauta", save_debrief=True)
    assert result["ok"] is True and result["restarting"] is True
    text = state_file.read_text()
    assert "ATC_AIRFIELD=Gudauta" in text
    assert "ATC_DEBUG=1" in text


def test_control_plane_run_pending_fires_exit(tmp_path):
    from map_server import ControlPlane
    state_file = tmp_path / ".control"
    calls = []
    cp = ControlPlane(["Kutaisi", "Gudauta"], state_file,
                      lambda *a: calls.append(a), "Kutaisi")
    cp.restart("Gudauta", save_debrief=False)
    assert calls == []          # nothing fires until the response is flushed
    cp.run_pending()
    assert calls == [("restart", "Gudauta", False)]
    cp.run_pending()            # idempotent: no second exit
    assert len(calls) == 1


def test_control_plane_stop(tmp_path):
    from map_server import ControlPlane
    state_file = tmp_path / ".control"
    calls = []
    cp = ControlPlane(["Kutaisi"], state_file, lambda *a: calls.append(a),
                      "Kutaisi")
    assert cp.stop()["ok"] is True
    cp.run_pending()
    assert calls == [("stop", "Kutaisi", False)]


def test_control_payload_in_snapshot(brain, airfield, tmp_path):
    from map_server import ControlPlane
    cp = ControlPlane(["Kutaisi", "Gudauta"], tmp_path / ".control",
                      lambda *a: None, "Kutaisi", debug=True)
    service = MapService(airfield, brain, FakeState([]), control=cp)
    control = service.snapshot()["control"]
    assert control["airfields"] == ["Gudauta", "Kutaisi"]
    assert control["current"] == "Kutaisi"
    assert control["debug"] is True


def test_snapshot_control_none_without_control(brain, airfield):
    assert MapService(airfield, brain, FakeState([])).snapshot()["control"] is None


def test_control_restart_endpoint(brain, airfield, tmp_path):
    from map_server import ControlPlane
    state_file = tmp_path / ".control"
    calls = []
    cp = ControlPlane(["Kutaisi", "Gudauta"], state_file,
                      lambda *a: calls.append(a), "Kutaisi", debug=True)
    service = MapService(airfield, brain, FakeState([]), control=cp)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/control/restart",
            data=json.dumps({"airfield": "Gudauta"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        # The current debug state is preserved when the caller omits it.
        assert calls == [("restart", "Gudauta", True)]
        assert "ATC_DEBUG=1" in state_file.read_text()
        # An unknown airfield is a 400 and does not exit.
        bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/control/restart",
            data=json.dumps({"airfield": "Batumi"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(bad)
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        assert len(calls) == 1
    finally:
        server.shutdown()


def test_control_endpoints_404_without_control(brain, airfield):
    service = MapService(airfield, brain, FakeState([]))  # no control plane
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/control/restart",
            data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req)
            assert False, "expected HTTP 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
