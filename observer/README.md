# Observer

Standalone, read-only DCS mission map prototype. It has its own code and web
client; its only dependency on the ATC project is the already-installed DCS
state bridge protocol (default TCP port 10309).

Run from the repository root:

```sh
./observer/start.sh --dcs-host 192.0.2.10 --srs-host 192.0.2.10
```

Open <http://localhost:8091>. DCS and SRS host/port, web port, polling rate,
and initial path history can be set on the command line.
For example, `--history 180` keeps up to three hours of trails in memory;
the map's Path minutes control can display a shorter window. No mission
commands are sent to DCS. The SRS connection in this first prototype performs
the client handshake only; selecting radio frequencies, receiving audio, and
transcription are future work.

The map uses the same dcsmaps Caucasus tile source and WMS overlay as the ATC
map: DCS tiles over an OpenStreetMap fallback, with airbases, cities, towns,
roads, railroads, landmarks, TACAN, VOR, NDB, DME, and database layers above.
The fallback remains visible where dcsmaps has no tile (including uncovered
low zooms). The **Map labels** checkbox toggles the WMS overlay, which refreshes
after map movement or zoom. Leaflet is loaded from its public CDN.

## Unit symbols

Aircraft and other units use MIL-STD-2525 / APP-6 SVG symbols rendered by the
independent `milsymbol` JavaScript library (pinned CDN version 3.0.4). DCS
blue, red, and neutral sides always render blue, red, and green, respectively.
Fixed-wing and
rotary-wing aircraft have distinct icons; ground and sea units use their
respective generic symbol sets. This is a display classification based on the
bridge's DCS category, not a detailed symbol database for each DCS aircraft
model. Markers shrink with zoom; at zoom levels below 8, nearby units of the
same coalition and domain are grouped into counted markers. Aircraft expand
into individual units at zoom 11; ground and sea units stay grouped until zoom
15. Observer hides unoccupied plane groups whose names contain `DynamicSlot`.
It also hides units explicitly marked inactive if the bridge supplies an
`active` field; the current bridge does not yet include that field. If a player
occupies a dynamic slot, the bridge player name keeps it visible in Real
Players. Blue and red coalition bullseyes are drawn as matching colored target
rings. The rest of the symbol code and map UI live under `observer/`.

The Units panel separates units with a non-empty bridge `player` name into
Real Players and puts units without a current player name under AI / no player.
Player paths are checked by default. A player joining, leaving, or changing
unit updates the lists and default path selection on the next bridge poll.
