"""Map view: airspace payload, live aircraft with phase, HTTP endpoints."""

import json
import threading
import time
import urllib.error
import urllib.request

import map_server
from brain import Controller, Phase
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
    arcs = af["checks"]["gate_arcs"]
    assert {arc["gate"] for arc in arcs} == set(af["gates"])
    assert all(arc["point"] == af["gates"][arc["gate"]]
               for arc in arcs)
    lead_zones = af["checks"]["arrival_handoff_zones"]
    assert {zone["gate"] for zone in lead_zones} == set(af["gates"])
    assert all(zone["angle_deg"] == 200 and zone["radius_nm"] == 5.0
               for zone in lead_zones)
    assert "25" in af["runways"]
    assert snap["aircraft"] == []


def test_snapshot_marks_join_instruction_for_live_zone_rendering(brain, airfield):
    brain.remember_speaker("Caveman", "Colt 1")
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.INBOUND
    pilot.entry_gate = "Entry East"
    pilot.arrival_join_issued = True
    service = MapService(airfield, brain, FakeState([_ac()]))
    aircraft = service.snapshot()["aircraft"]
    assert len(aircraft) == 1
    assert aircraft[0]["entry_gate"] == "Entry East"
    assert aircraft[0]["arrival_join_issued"] is True


def test_retained_debrief_recovers_entry_for_handoff_zone(brain):
    aircraft = MapService._retained_entry("Colt 1", [], [{
        "kind": "tx", "controller": "control",
        "text": "Colt 1, Control, turn right heading 150 to join via Entry East",
    }])
    assert aircraft["entry_gate"] == "Entry East"
    assert aircraft["review_handoff_zone"] is True


def test_snapshot_includes_wind_arrow_at_tower(brain, airfield):
    brain.set_wind(30, 4)
    service = MapService(airfield, brain, FakeState([]))
    af = service.snapshot()["airfield"]
    assert af["wind"]["dir"] == 30
    assert af["wind"]["speed"] == 8  # 4 m/s from DCS, shown in knots
    assert af["wind"]["text"] == "030, 8 knots"
    assert af["tower_pos"] == [airfield.tower_pos[0], airfield.tower_pos[1]]
    assert af["topgun"]["radius_m"] == 200


def test_snapshot_uses_live_dcs_tower_when_unconfigured(brain, airfield):
    airfield.tower_pos = None

    class WithTower(FakeState):
        def tower(self, name):
            return {"name": name, "lat": 42.1789, "lon": 42.4811}

    brain.set_wind(270, 2)
    service = MapService(airfield, brain, WithTower([]))
    af = service.snapshot()["airfield"]
    assert af["tower_pos"] == [42.1789, 42.4811]
    assert af["topgun"]["radius_m"] == 200


def test_snapshot_rejects_tower_off_the_field(brain, airfield):
    """Caucasus origin (Crimea) must not become the wind-arrow base."""
    airfield.tower_pos = None

    class OriginTower(FakeState):
        def tower(self, name):
            return {"name": name, "lat": 45.129, "lon": 34.266}

    service = MapService(airfield, brain, OriginTower([]))
    lat, lon = service.snapshot()["airfield"]["tower_pos"]
    rwy = list(airfield.runways.values())
    assert lat == (rwy[0][0] + rwy[1][0]) / 2
    assert lon == (rwy[0][1] + rwy[1][1]) / 2


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


def test_debrief_filename_includes_player_and_callsign():
    from map_server import debrief_filename
    name = debrief_filename("Kutaisi", when=0, player="Caveman",
                            callsign="Adder 1")
    assert name.startswith("tracks_kutaisi_caveman_adder-1_")
    assert name.endswith(".json")


def test_spawn_debrief_is_one_file_per_pilot_spawn(brain, airfield, tmp_path):
    """Each DCS spawn writes its own debrief, named with the player."""
    brain.sync_speakers([("Caveman", "Adder11")])
    service = MapService(airfield, brain, FakeState([]), debrief_dir=tmp_path)
    service.begin_spawn("Caveman", "Adder 1")
    service.tracks.update("Adder 1", 42.18, 42.50, 1.0, 1000.0)
    service.save_tracks()
    first = list(tmp_path.glob("tracks_*.json"))
    assert len(first) == 1
    assert "caveman" in first[0].name
    payload = json.loads(first[0].read_text())
    assert payload["player"] == "Caveman"
    assert "Adder 1" in payload["tracks"]

    service.end_spawn("Caveman")
    brain.sync_speakers([("Caveman", "Adder21")])
    service.begin_spawn("Caveman", "Adder 2")
    service.tracks.update("Adder 2", 42.19, 42.51, 1.0, 2000.0)
    service.save_tracks()
    files = list(tmp_path.glob("tracks_*.json"))
    assert len(files) == 2
    names = {p.name for p in files}
    assert any("adder-1" in n for n in names)
    assert any("adder-2" in n for n in names)


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
    assert "Airbases" in bm["wms"]["layers"]
    assert "mapserv" in bm["wms"]["url"]


def test_basemap_choice_override(brain, airfield):
    snap = MapService(airfield, brain, FakeState([]), basemap="osm").snapshot()
    bm = snap["airfield"]["basemap"]
    assert bm["name"] == "osm"
    assert "openstreetmap" in bm["url"]
    assert bm["tms"] is False
    assert "wms" not in bm


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


def test_chatter_callsign_from_text_before_learned(brain, airfield):
    # Before the speaker mapping is learned, the callsign named in the text is
    # used, so the first exchange is not split onto a ghost trail.
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1", "ground")
    assert service.chatter()[-1]["callsign"] == "Colt 1"


def test_chatter_coach_note_lands_in_the_pilot_debrief(brain, airfield):
    # Coach mode delivers a wording note to ONE pilot as a direct in-game
    # message and as a "coach" chatter entry: the note must carry that flight's
    # callsign, so (a) the per-spawn debrief includes it (_filter_chatter),
    # (b) it is pinned to the pilot's position on their trail and (c) it does
    # NOT leak into another pilot's debrief.
    service = MapService(airfield, brain, FakeState([]))
    service.log_chatter("coach", 0.0, "Colt 1",
                        "Apollo suggests: requesting taxi.", "coach")
    entry = service.chatter()[-1]
    assert entry["kind"] == "coach"
    assert entry["callsign"] == "Colt 1"
    theirs = service._filter_chatter("Caveman", "Colt 1")
    assert any(e["kind"] == "coach" and "requesting taxi" in e["text"]
               for e in theirs)
    other = service._filter_chatter("Oscar", "Ford 2")
    assert not [e for e in other if e["kind"] == "coach"]


def test_chatter_uses_live_position_when_no_trail(brain, airfield):
    # The very first exchange has no trail point yet; the call must still be
    # pinned to the pilot's live position (asked from the state client).
    service = MapService(airfield, brain, FakeState([_ac(lat=42.19, lon=42.51)]))
    service.log_chatter("rx", 250.0, "Caveman", "Ground, Colt 1", "ground")
    comms = service.tracks.comms("Colt 1")
    assert comms and comms[-1]["lat"] == 42.19 and comms[-1]["lon"] == 42.51


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


def test_prune_debriefs_keeps_newest(tmp_path):
    """Debriefs are saved every session, so old ones must be pruned."""
    import os
    import time as _time
    for i in range(5):
        p = tmp_path / f"tracks_k_{i}.json"
        p.write_text("{}")
        os.utime(p, (1000 + i, 1000 + i))   # deterministic mtimes
    map_server.prune_debriefs(keep=2, directory=tmp_path)
    left = sorted(p.name for p in tmp_path.glob("tracks_*.json"))
    assert left == ["tracks_k_3.json", "tracks_k_4.json"]


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


def test_replay_view_keeps_saved_chatter_while_live_chatter_records(
        brain, airfield, tmp_path):
    tracks_file = tmp_path / "tracks.json"
    (tracks_file).write_text(json.dumps(
        {"tracks": {"Colt 1": [[42.18, 42.50, 100.0], [42.19, 42.51, 200.0]]},
         "comms": {}, "chatter": [{"kind": "tx", "text": "old", "who": "",
                                   "callsign": "", "controller": "tower",
                                   "freq": 263.0, "t": "10:00:00"}]}))
    service = MapService(airfield, brain, None, tracks_file=tracks_file)
    assert service.replay is True
    saved_chatter = service.snapshot()["chatter"]
    service.log_chatter("rx", 263.0, "Caveman", "live call")
    assert service.chatter()[-1]["text"] == "live call"
    assert service.snapshot()["chatter"] == saved_chatter


def test_selecting_debrief_does_not_interrupt_live_spawn_recording(
        brain, airfield, tmp_path, monkeypatch):
    monkeypatch.setattr(map_server, "DEBRIEF_DIR", tmp_path)
    replay_file = tmp_path / "tracks_saved.json"
    replay_file.write_text(json.dumps({
        "tracks": {"Old sortie": [[42.18, 42.50, 100.0]]},
        "comms": {},
        "chatter": [{"kind": "tx", "text": "saved call", "who": "",
                     "callsign": "", "controller": "tower",
                     "freq": 270.0, "t": "10:00:00"}],
    }))
    brain.remember_speaker("Caveman", "Colt 1")
    service = MapService(airfield, brain, FakeState([]), debrief_dir=tmp_path)
    service.begin_spawn("Caveman", "Colt 1")
    service.record([_ac(lat=42.18, lon=42.50)])
    assert service.load_debrief(replay_file.name)["ok"]
    saved_view_chatter = service.snapshot()["chatter"]

    # Live recording and save continue behind the selected replay view.
    service.record([_ac(lat=42.20, lon=42.52)])
    service.log_chatter("rx", 270.0, "Caveman", "live call", "tower")
    service.save_tracks()
    live_file = next(tmp_path.glob("tracks_kutaisi_caveman_colt-1_*.json"))
    live_debrief = json.loads(live_file.read_text())
    assert len(live_debrief["tracks"]["Colt 1"]) >= 2
    assert any(event["text"] == "live call"
               for event in live_debrief["chatter"])
    assert service.snapshot()["chatter"] == saved_view_chatter

    service.unload_debrief()
    live_snapshot = service.snapshot()
    assert live_snapshot["replay"] is False
    assert any(ac["callsign"] == "Colt 1" for ac in live_snapshot["aircraft"])


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
        _assert_picture_overlay_review_contract(js)
    finally:
        server.shutdown()


def test_viewer_serves_repaired_picture_overlay_asset(airfield, tmp_path):
    import base64
    from viewer import ViewerService, make_handler as make_viewer_handler

    service = ViewerService(airfield, tmp_path, None,
                            credentials={"reviewer": "secret"})
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_viewer_handler(service))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = base64.b64encode(b"reviewer:secret").decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/map.js",
            headers={"Authorization": f"Basic {token}"})
        js = urllib.request.urlopen(request).read()
        _assert_picture_overlay_review_contract(js)
    finally:
        server.shutdown()


def _assert_picture_overlay_review_contract(js):
    """Guard the shared live-map/viewer review interactions in served JS.

    This is an asset-level regression check, not a substitute for exercising
    Leaflet in a browser. It catches accidental removal or rewiring of the
    click/popup/compass contract for both routes that serve map.js.
    """
    assert b"Possible STT repair" in js
    assert b"STT reading" in js
    assert b"Actual ${call.point}:" in js
    assert b"actualAngelsAt" in js
    assert b"name === 'kutasi'" in js

    # All arrow parts and the actual-position ring are independently clickable.
    assert b"`${popupKeyBase}|line`" in js
    assert b"`${popupKeyBase}|head:${side}`" in js
    assert b"`${popupKeyBase}|dot`" in js
    assert b"bindBullseyePopup(ring, popupKey, popupText, showCompass)" in js

    # The popup is anchored where the user clicked; redraws don't recenter it
    # on the arrow midpoint. Its own remove lifecycle clears popup/compass state.
    draw_calls = js.split(b"function drawBullseyeCalls", 1)[1].split(
        b"function bindBullseyePopup", 1)[0]
    assert b"bullseyeLayer.clearLayers()" in draw_calls  # each poll redraws
    assert b".setLatLng(" not in draw_calls  # redraw never moves an open popup
    assert b".setLatLng(event.latlng)" in js
    assert b"popup.on('remove'" in js
    assert b"if (bullseyePopup === popup)" in js
    assert b"bullseyePopupKey = null;" in js
    assert b"clearBullseyeCompass();" in js

    # Compass inspection is triggered from the ring popup click and is drawn
    # from the BRA reference (bullseye or named marking point) to actual pos.
    assert b"const showCompass = () => drawActualBullseyeCompass(" in js
    assert b"bearingRange(call.lat, call.lon, c.lat, c.lon)" in js
    assert b"call.lat, call.lon, c.lat, c.lon, actualBRA[0]" in js
    assert b"popup.openOn(map);\n    if (onClick) onClick();" in js
    assert b"const bull = [bullLat, bullLon];" in js
    assert b"const actual = [actualLat, actualLon];" in js
    assert b"destMeters(bullLat, bullLon, 0, 10 * 1852)" in js
    assert b"const arc = [];" in js


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
        # The exit fires *after* the response is flushed (so the browser sees
        # the result), so wait for the callback rather than racing it.
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
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


# ---------- Debrief save robustness + timed autosave ----------

def test_save_sanitises_non_serialisable_payload(brain, airfield, tmp_path):
    """A stray non-serialisable value is coerced, so the save still succeeds."""
    from collections import deque
    messages = []
    service = MapService(airfield, brain, FakeState([]),
                         tracks_file=tmp_path / "tracks.json",
                         log=messages.append)
    # A set is not JSON-serialisable; _json_safe coerces it to a list, so the
    # save still succeeds (and nothing is logged).
    service.tracks._tracks["Colt 1"] = deque([(42.18, 42.50, 100.0)])
    service.tracks._comms["Colt 1"] = deque(
        [{"kind": "rx", "text": "hi", "tags": {"a", "b"}}])
    service.save_tracks()
    assert (tmp_path / "tracks.json").exists()
    assert messages == []


def test_save_reports_write_failure(brain, airfield, tmp_path):
    """A write error is reported through the bot's log() (with a traceback)."""
    messages = []
    # A directory where the file should be makes os.replace/write fail.
    bad = tmp_path / "tracks.json"
    bad.mkdir()
    service = MapService(airfield, brain, FakeState([]),
                         tracks_file=bad, log=messages.append)
    service.tracks.update("Colt 1", 42.18, 42.50, 2.0, 100.0)
    service.save_tracks()
    assert any("debrief" in m for m in messages)


def test_start_autosave_writes_on_a_timer(brain, airfield, tmp_path):
    """The periodic timer saves the trail even with no record()/snapshot()."""
    tracks_file = tmp_path / "tracks.json"
    service = MapService(airfield, brain, FakeState([]),
                         tracks_file=tracks_file)
    service.tracks.update("Colt 1", 42.18, 42.50, 2.0, 100.0)
    service.start_autosave(interval=0.05)
    deadline = time.time() + 2.0
    while time.time() < deadline and not tracks_file.exists():
        time.sleep(0.02)
    assert tracks_file.exists()
    assert "Colt 1" in json.loads(tracks_file.read_text())["tracks"]


def test_start_autosave_noop_for_replay(brain, airfield, tmp_path):
    """A replay (or no tracks_file) must not start a writing timer."""
    tracks_file = tmp_path / "tracks.json"
    tracks_file.write_text(json.dumps(
        {"tracks": {"Colt 1": [[42.18, 42.50, 100.0]]}, "comms": {}}))
    service = MapService(airfield, brain, None, tracks_file=tracks_file)
    assert service.replay is True
    service.start_autosave(interval=0.05)  # must be a no-op
    before = tracks_file.read_text()
    time.sleep(0.2)
    assert tracks_file.read_text() == before  # untouched
