# Observer

Standalone, read-only DCS mission map prototype. It has its own code and web
client; its only dependency on the ATC project is the already-installed DCS
state bridge protocol (default TCP port 10309).

Run from the repository root:

```sh
./observer/start.sh --dcs-host 192.0.2.10 --srs-host 192.0.2.10
```

Open <http://localhost:8091>. DCS and SRS host/port, web port, polling rate,
coalition bullseye, and initial path history can be set on the command line.
For example, `--history 180` keeps up to three hours of trails in memory;
the map's Path minutes control can display a shorter window. No mission
commands are sent to DCS. The SRS connection in this first prototype performs
the client handshake only; selecting radio frequencies, receiving audio, and
transcription are future work.

The page uses OpenStreetMap tiles and Leaflet from their public CDNs.
