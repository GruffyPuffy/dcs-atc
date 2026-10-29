# Pilot state machine

GitHub renders this Mermaid flowchart directly in Markdown. It shows the
trainer's broad flight phases and the events that move the pilot through them.

```mermaid
flowchart TD
    Spawn([Spawn]) --> Idle[Idle / start-up]
    Idle --> Clearance[Ground: departure clearance]
    Clearance --> Taxi[Taxi]
    Taxi --> Holding[Holding short]
    Holding --> Lineup[Tower: line up]
    Lineup --> Takeoff[Takeoff]
    Takeoff -->|Pass outbound exit arc; Tower auto-pushes Control| Airborne[Airborne / Control]
    Airborne -->|Check in, climb clear of CTR, climb to Angels 15| Stingray[Stingray]
    Stingray -->|Pilot requests RTB; delayed auto-push within 50 NM| ReturnControl[Control]
    ReturnControl --> Inbound[Inbound: heading and assigned entry]
    Inbound -->|Enter assigned lead-in sector while closing| TowerPush[Control auto-pushes Tower]
    Inbound -. missed sector .-> ArcFallback[Entry arc fallback]
    ArcFallback --> TowerPush
    Inbound -. missed arc .-> CTRFallback[CTR fallback]
    CTRFallback --> TowerPush
    TowerPush -->|Pilot checks in and reports assigned entry| Landing[Tower / Landing]
    Landing -->|Runway in sight| Break[Overhead break]
    Break -->|On final| Land[Landing clearance / land]
    Land -->|Vacate runway; Tower auto-pushes Ground| PostLandingTaxi[Taxi to parking]
    PostLandingTaxi --> Parked([Parked])
```

## Two things to keep separate

- **Flight phase** is the aircraft's progress through the sortie: takeoff,
  airborne, inbound, landing, and so on.
- **Checked-in agency** is the radio controller the pilot is currently talking
  to. A handoff message creates a pending transfer; the pilot completes it by
  switching and checking in on the new frequency.

The aircraft can be physically airborne while still checked in with Tower. For
example, the trainer marks the departure phase as **Airborne** when it detects
the outbound exit crossing, and then Tower issues the Control handoff.

The **entry lead-in sector** is the early Control-to-Tower trigger. It is not
the entry arc: if the aircraft misses the sector, crossing the arc and entering
the CTR are later fallbacks. See [Zones and automatic handoffs](zones-and-handoffs.md)
for the map geometry.
