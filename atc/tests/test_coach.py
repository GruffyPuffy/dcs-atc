"""Coach: wording suggestions (coach mode only) — intent handling untouched."""

from brain import AtcBrain, Controller
import coach


# ---------- coach.note (unit) ----------

def test_note_names_the_canonical_taxi_call():
    for utterance in ("give me taxi",
                      "i need taxi",
                      "i want taxi to the ramp",
                      "can i have taxi",
                      "can i get taxi",
                      "let me have taxi"):
        assert coach.note(utterance) == "requesting taxi", utterance


def test_note_ready_for_takeoff_becomes_departure():
    assert coach.note("ready for takeoff") == "ready for departure"
    assert coach.note("ready to go") == "ready for departure"


def test_note_is_silent_on_proper_calls():
    assert coach.note("requesting taxi") == ""
    assert coach.note("request taxi") == ""
    assert coach.note("i need to request taxi") == ""  # request word present


def test_note_is_silent_on_unrelated_text():
    assert coach.note("") == ""
    assert coach.note("runway vacated") == ""
    # "inbound" is not a coach rule target — nothing to say:
    assert coach.note("inbound") == ""
    assert coach.note("ready for departure") == ""


# ---------- brain integration ----------

def test_coach_note_pends_in_coach_mode(brain):
    # The note is NOT part of the radio reply: the reply is the plain ATC
    # transmission, and the note waits as a one-shot on the pilot state for
    # the caller to deliver as a direct in-game message.
    reply = brain.handle("Ground, Colt 1, give me taxi",
                         controller=Controller.GROUND)
    assert reply and "cleared taxi" in reply
    assert "suggests" not in reply
    assert "Apollo suggests: requesting taxi." in brain.take_coach("Colt 1")
    assert brain.take_coach("Colt 1") == ""  # one-shot: taken and cleared


def test_coach_remains_silent_on_proper_calls(brain):
    brain.handle("Ground, Colt 1, requesting taxi",
                 controller=Controller.GROUND)
    assert brain.take_coach("Colt 1") == ""


def test_casual_mode_never_sets_a_note():
    # Casual never asks the coach: the pending note is empty, so nothing is
    # ever delivered. Two fresh brains (the call advances the phase, so one
    # state machine cannot replay the same transmission twice).
    coach_brain, casual_brain = AtcBrain(), AtcBrain()
    casual_brain.mode = "casual"
    coach_brain.handle("Ground, Ford 2, give me taxi",
                       controller=Controller.GROUND)
    casual_brain.handle("Ground, Ford 2, give me taxi",
                        controller=Controller.GROUND)
    assert "Apollo suggests" in coach_brain.take_coach("Ford 2")
    assert casual_brain.take_coach("Ford 2") == ""


def test_say_again_replays_the_clearance_without_the_note(brain):
    brain.handle("Ground, Colt 1, give me taxi", controller=Controller.GROUND)
    replay = brain.handle("Colt 1, say again", controller=Controller.GROUND)
    assert replay and "cleared taxi" in replay
    # The note belongs to the ORIGINAL transmission only.
    assert brain.take_coach("Colt 1") == ""


def test_no_note_on_readback_feedback(brain):
    # "readback incomplete" already corrected this transmission; the coach
    # never stacks a wording note on it.
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    # Sloppy AND incomplete: only the semantic feedback speaks.
    reply = brain.handle("Colt 1, I need, hold short",
                         controller=Controller.GROUND)
    assert "readback incomplete" in reply.lower()
    assert brain.take_coach("Colt 1") == ""


def test_reset_clears_a_pending_note(brain):
    brain.handle("Ground, Colt 1, give me taxi", controller=Controller.GROUND)
    assert brain.take_coach("Colt 1")
    brain.handle("Ground, Colt 1, give me taxi", controller=Controller.GROUND)
    brain.handle("Colt 1, reset", controller=Controller.GROUND)
    assert brain.take_coach("Colt 1") == ""


# ---------- readback-specific note: callsign LEADING a readback ----------

def test_note_readback_fires_on_a_leading_callsign():
    assert coach.note_readback(
        "colt 1, cleared taxi sierra echo and hold short runway 25",
        "Colt 1")
    assert coach.note_readback(
        "tower, colt 1, line up and wait runway 25", "Colt 1")


def test_note_readback_is_silent_on_the_sop_form():
    # Content first, callsign LAST — exactly the SOP readback.
    assert coach.note_readback(
        "cleared taxi sierra echo and hold short runway 25, colt 1",
        "Colt 1") == ""


def test_note_readback_is_silent_on_non_readback_calls():
    # Only real readbacks reach note_readback; these are other intents — but
    # the guard still holds even without the brain flag.
    assert coach.note_readback("Ground, Colt 1, requesting taxi",
                               "Colt 1") == ""
    assert coach.note_readback("Tower, Colt 1, at runway 25, "
                               "ready for departure", "Colt 1") == ""
    assert coach.note_readback("Colt 1", "Colt 1") == ""


def test_leading_callsign_readback_gets_a_note_in_the_feed(brain):
    # The SOP readback never sets a note; the leading-callsign habit gets the
    # same resolution the bot already gives it (the call IS handled — silent
    # ack or a "readback correct") plus a teach note in the pilot's in-game
    # feed. The radio hears NOTHING from the coach either way.
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    assert brain.take_coach("Colt 1") == ""
    sop = brain.handle("Cleared taxi Sierra Echo and hold short runway 25, "
                       "Colt 1", controller=Controller.GROUND)
    assert "readback" not in sop.lower() or "readback correct" in sop.lower()
    assert brain.take_coach("Colt 1") == ""
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    habit = brain.handle("Colt 1, cleared taxi Sierra Echo and hold short "
                         "runway 25", controller=Controller.GROUND)
    # The ATC side is unchanged whatever it says ("readback correct" when the
    # first readback was already clicked, silent confirmation otherwise).
    assert habit == "" or habit.endswith("readback correct.")
    assert "ends with your callsign" in brain.take_coach("Colt 1")


def test_note_survives_only_one_transmission(brain):
    # Regression: the pending note of one transmission must expire with it.
    brain.handle("Ground, Colt 1, give me taxi", controller=Controller.GROUND)
    note = brain.take_coach("Colt 1")
    assert "requesting taxi" in note
    # Next transmission (no new coach rule hit) expires the old note.
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    assert brain.take_coach("Colt 1") == ""
