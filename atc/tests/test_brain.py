"""Brain: intent recognition, controller routing, per-pilot state machine."""

from brain import Controller, Phase


# ---------- Ground ----------

def test_ground_clearance_assigns_exit_gate(brain):
    reply = brain.handle("Ground, Colt 1, ready to copy clearance",
                         controller=Controller.GROUND)
    assert "Colt 1" in reply
    assert "Kutaisi Ground" in reply
    assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE
    assert brain.pilots["Colt 1"].exit_gate


def test_ground_taxi(brain):
    reply = brain.handle("Ground, Colt 1, requesting taxi",
                         controller=Controller.GROUND)
    assert "taxi to runway" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAXI


def test_ground_hold_short_hands_off_to_tower(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Colt 1, holding short runway 25",
                         controller=Controller.GROUND)
    assert "contact" in reply.lower()
    assert "Tower" in reply
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


# ---------- Tower ----------

def test_tower_takeoff(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_tower_runway_in_sight_overhead(brain):
    reply = brain.handle("Tower, Colt 1, runway in sight",
                         controller=Controller.TOWER)
    assert "overhead break" in reply


def test_tower_inbound_without_track(brain):
    reply = brain.handle("Tower, Colt 1, inbound", controller=Controller.TOWER)
    assert "Colt 1" in reply
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


# ---------- Control ----------

def test_control_inbound_routes_via_entry_gate(brain):
    reply = brain.handle("Control, Colt 1, inbound 35 miles north",
                         controller=Controller.CONTROL)
    assert "Kutaisi Control" in reply
    assert "radar contact" in reply
    assert "Entry" in reply
    assert brain.pilots["Colt 1"].entry_gate


# ---------- Shared / fallback ----------

def test_unknown_request_says_again(brain):
    reply = brain.handle("Tower, Colt 1, banana banana",
                         controller=Controller.TOWER)
    assert "say again" in reply


def test_no_callsign_returns_none(brain):
    assert brain.handle("requesting taxi", controller=Controller.GROUND) is None


def test_roger_on_readback(brain):
    reply = brain.handle("Tower, Colt 1, wilco", controller=Controller.TOWER)
    assert "roger" in reply.lower()


# ---------- Per-pilot isolation ----------

def test_two_pilots_have_independent_state(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Ground, Ford 2, ready to copy clearance",
                 controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].phase == Phase.TAXI
    assert brain.pilots["Ford 2"].phase == Phase.CLEARANCE


# ---------- Runway / go-around ----------

def test_go_around_fires_once(brain):
    first = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    second = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert "go around" in first
    assert second is None  # only once per approach


def test_go_around_resets_when_not_on_final(brain):
    brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    brain.check_final("Colt 1", on_final=False, runway_occupied=False)
    again = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert "go around" in again


# ---------- Wind / runway ----------

def test_set_wind_and_runway(brain):
    brain.set_wind(270, 5)
    assert brain.wind == "270 at 5"
    brain.set_wind(0, 0)
    assert brain.wind == "calm"
    brain.set_runway("07")
    assert brain.runway == "07"


# ---------- Help (trainer aid) ----------

def test_help_idle_points_to_ground(brain):
    reply = brain.handle("Colt 1 help", controller=Controller.TOWER)
    assert "Ground" in reply
    assert "clearance" in reply.lower()


def test_help_after_taxi_points_to_hold_short(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Colt 1 help", controller=Controller.GROUND)
    assert "holding short" in reply.lower()


def test_help_holding_points_to_tower(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Colt 1, holding short runway 25", controller=Controller.GROUND)
    reply = brain.handle("Colt 1 help", controller=Controller.GROUND)
    assert "Tower" in reply
    assert "ready for departure" in reply.lower()


def test_help_works_on_any_controller(brain):
    # help is a shared intent, valid regardless of which frequency it arrives on
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        reply = brain.handle("Colt 1 help", controller=controller)
        assert reply and "Colt 1" in reply


def test_help_requires_callsign(brain):
    assert brain.handle("help me please", controller=Controller.TOWER) is None
