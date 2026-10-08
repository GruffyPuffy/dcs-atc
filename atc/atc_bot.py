"""ATC trainer: listen on SRS, STT, rules-based reply, TTS, transmit back.

Frequency, tower name and active runway come from airspace.json (per airfield);
CLI flags override them.

Usage:
    uv run atc_bot.py [--airfield Kutaisi] [--airspace airspace.json]
                      [--host IP] [--port 5002] [--freq 263.0] [--name ATC]
                      [--eam atc] [--stt-model small.en] [--voice en_US-amy-medium]
                      [--ground-voice en_US-ryan-medium]
                      [--control-voice en_US-lessac-medium]
                      [--atis-voice en_GB-alan-medium]
                      [--log /tmp/atc_log.txt] [--keep 10] [--gain 1.0]
                      [--state-host 127.0.0.1] [--state-port 10309] [--no-state]
"""

import argparse
import datetime
import math
import threading
import time
import wave
from pathlib import Path

import av
import numpy as np

from airspace import Airspace
from atis import build_atis
from brain import AtcBrain, Controller, Phraseology
from callsigns import CallsignRegistry
from ctr import CtrEvent, CtrTracker
from srs_client import SrsClient
from state_client import StateClient
from workers import ControllerWorker, SharedState

SAMPLE_RATE = 48000
STT_RATE = 16000
MIN_TRANSMISSION_SECONDS = 0.3


def resample_16k(pcm: bytes) -> np.ndarray:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=STT_RATE)
    frame = av.AudioFrame.from_ndarray(
        np.frombuffer(pcm, dtype=np.int16).reshape(1, -1),
        format="s16", layout="mono")
    frame.sample_rate = SAMPLE_RATE
    out = resampler.resample(frame)
    if not out:
        return np.zeros(0, dtype=np.float32)
    data = b"".join(bytes(f.planes[0]) for f in out)
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def resample_to_48k(pcm: bytes, rate: int) -> bytes:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    frame = av.AudioFrame.from_ndarray(
        np.frombuffer(pcm, dtype=np.int16).reshape(1, -1),
        format="s16", layout="mono")
    frame.sample_rate = rate
    out = resampler.resample(frame)
    return b"".join(bytes(f.planes[0]) for f in out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airfield", default="Kutaisi",
                        help="airfield key in airspace.json (drives freq/name/runway)")
    parser.add_argument("--airspace", default=None,
                        help="path to airspace.json (default: next to this file)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--freq", type=float, default=None,
                        help="MHz, AM (default: from airspace.json)")
    parser.add_argument("--atis-freq", type=float, default=None,
                        help="ATIS MHz, AM (default: from airspace.json; 0 disables)")
    parser.add_argument("--ground-freq", type=float, default=None,
                        help="Ground MHz, AM (default: from airspace.json; 0 disables)")
    parser.add_argument("--control-freq", type=float, default=None,
                        help="Control MHz, AM (default: from airspace.json; 0 disables)")
    parser.add_argument("--atis-interval", type=float, default=60.0,
                        help="seconds between ATIS broadcasts (0 disables)")
    parser.add_argument("--name", default=None,
                        help="SRS client name (default: airfield tower name)")
    parser.add_argument("--eam", default=None, help="External AWACS Mode password")
    parser.add_argument("--stt-model", default="small.en")
    parser.add_argument("--voice", default="en_US-amy-medium",
                        help="Piper voice for Tower (default: en_US-amy-medium)")
    parser.add_argument("--ground-voice", default="en_US-ryan-medium",
                        help="Piper voice for Ground (default: en_US-ryan-medium)")
    parser.add_argument("--control-voice", default="en_US-lessac-medium",
                        help="Piper voice for Control (default: en_US-lessac-medium)")
    parser.add_argument("--atis-voice", default="en_GB-alan-medium",
                        help="Piper voice for ATIS (default: en_GB-alan-medium)")
    parser.add_argument("--log", default="/tmp/atc_log.txt")
    parser.add_argument("--audio-dir", default="/tmp/atc_audio")
    parser.add_argument("--keep", type=int, default=10)
    parser.add_argument("--gain", type=float, default=1.0)
    parser.add_argument("--speech-rate", type=float, default=0.7,
                        help="Piper length_scale; lower = faster (0.6-1.0)")
    parser.add_argument("--state-host", default="127.0.0.1")
    parser.add_argument("--state-port", type=int, default=10309)
    parser.add_argument("--no-state", action="store_true",
                        help="disable the DCS state bridge (no live positions)")
    parser.add_argument("--map-port", type=int, default=0,
                        help="serve the live map view on this port (0 disables)")
    parser.add_argument("--map-host", default="0.0.0.0",
                        help="bind address for the map view")
    parser.add_argument("--debug", action="store_true",
                        help="log routing/state detail for every transmission")
    args = parser.parse_args()

    from faster_whisper import WhisperModel
    from piper import PiperVoice
    from piper.config import SynthesisConfig

    # The airfield config is the single source of truth: frequency, tower name
    # and active runway all come from airspace.json. CLI flags override.
    airspace = Airspace.load(args.airspace) if args.airspace else Airspace.load()
    airfield = airspace.get(args.airfield)
    if airfield is None:
        raise SystemExit(f"airfield {args.airfield!r} not found in airspace.json")
    freq_mhz = args.freq if args.freq is not None else airfield.frequency_mhz
    if not freq_mhz:
        raise SystemExit(f"airfield {args.airfield!r} has no frequency_mhz in airspace.json")
    name = args.name if args.name is not None else airfield.tower
    atis_mhz = args.atis_freq if args.atis_freq is not None else airfield.atis_frequency_mhz
    ground_mhz = args.ground_freq if args.ground_freq is not None else airfield.ground_frequency_mhz
    control_mhz = args.control_freq if args.control_freq is not None else airfield.control_frequency_mhz

    freq_hz = round(freq_mhz * 1_000_000)
    atis_hz = round(atis_mhz * 1_000_000) if atis_mhz else None
    ground_hz = round(ground_mhz * 1_000_000) if ground_mhz else None
    control_hz = round(control_mhz * 1_000_000) if control_mhz else None
    # frequency (Hz) -> controller role, for routing incoming transmissions
    controller_by_freq = {freq_hz: Controller.TOWER}
    if ground_hz:
        controller_by_freq[ground_hz] = Controller.GROUND
    if control_hz:
        controller_by_freq[control_hz] = Controller.CONTROL
    log_path = Path(args.log)
    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Loading Whisper '{args.stt_model}'...")
    stt = WhisperModel(args.stt_model, device="cpu", compute_type="int8")
    # A distinct voice per controller adds immersion: Tower, Ground, Control and
    # ATIS each sound like a different controller.
    voice_names = {
        Controller.TOWER: args.voice,
        Controller.GROUND: args.ground_voice,
        Controller.CONTROL: args.control_voice,
    }
    voices: dict[str, PiperVoice] = {}
    for voice_name in dict.fromkeys([*voice_names.values(), args.atis_voice]):
        print(f"[*] Loading Piper voice '{voice_name}'...")
        voices[voice_name] = PiperVoice.load(f"voices/{voice_name}.onnx")
    voice_for = {ctrl: voices[v] for ctrl, v in voice_names.items()}
    atis_voice = voices[args.atis_voice]

    # The brain uses live positions to make inbound replies distance-aware and
    # to warn on unannounced CTR entry / occupied-runway go-arounds.
    state = None if args.no_state else StateClient(args.state_host, args.state_port)
    callsigns = CallsignRegistry()
    if state is not None:
        try:
            slots = state.callsigns()
            if slots:
                callsigns = CallsignRegistry.from_mission(slots)
                print(f"[*] Callsigns from mission: {len(callsigns.names)} flights "
                      f"({', '.join(callsigns.names[:8])}...)")
        except (OSError, RuntimeError) as error:
            print(f"[*] Could not read mission callsigns ({error}); using defaults")
    brain = AtcBrain(tower=airfield.tower, runway=airfield.active_runway,
                     phraseology=Phraseology.load(), callsigns=callsigns,
                     ground=airfield.ground, control=airfield.control,
                     gates=list(airfield.gates), gate_locator=airfield.nearest_gate,
                     airfield=airfield)
    tracker = CtrTracker(airfield)
    print(f"[*] Airfield {airfield.name}: {airfield.tower}, {freq_mhz:.3f} MHz, "
          f"runway {airfield.active_runway}, CTR ceiling "
          f"{airfield.ctr.ceiling_ft_agl:.0f} ft AGL")
    print("[*] Ready.")

    def log(line: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"[{stamp}] {line}"
        print(text, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def debug(line: str) -> None:
        """Extra routing/state detail, only when --debug is set."""
        if args.debug:
            log(f"DBG {line}")

    def apply_gain(pcm: bytes) -> bytes:
        if args.gain == 1.0:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
        return np.clip(samples * args.gain, -32768, 32767).astype(np.int16).tobytes()

    def peak_dbfs(pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if not samples.size:
            return -120.0
        peak = float(np.abs(samples).max()) / 32768.0
        return 20 * math.log10(max(peak, 1e-6))

    def save_wav(pcm: bytes) -> Path | None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = audio_dir / f"rx_{stamp}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        if args.keep > 0:
            wavs = sorted(audio_dir.glob("rx_*.wav"), key=lambda p: p.stat().st_mtime)
            for old in wavs[:-args.keep]:
                old.unlink(missing_ok=True)
        return path

    # Piper mispronounces some names; spell them phonetically for synthesis
    # but log the real text.
    PRONUNCIATION = {
        "Kutaisi": "koo-tie-see",
        "Batumi": "bah-too-me",
    }

    # Piper synthesis and the SRS tx queue are shared across controller
    # workers, so serialise them (and the tx wav write) with a lock.
    tts_lock = threading.Lock()

    def speak(text: str, freq: int = freq_hz, voice=None,
              controller: Controller | None = None) -> None:
        """TTS the reply in the given voice and transmit it on the frequency."""
        spoken = text
        for word, phonetic in PRONUNCIATION.items():
            spoken = spoken.replace(word, phonetic)
        voice = voice or voice_for[Controller.TOWER]
        with tts_lock:
            syn_config = SynthesisConfig(length_scale=args.speech_rate)
            chunks = list(voice.synthesize(spoken, syn_config=syn_config))
            rate = chunks[0].sample_rate
            pcm22k = b"".join(c.audio_int16_bytes for c in chunks)
            pcm48k = resample_to_48k(pcm22k, rate)
            client.transmit(pcm48k, freq)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            with wave.open(str(audio_dir / f"tx_{stamp}.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm48k)
        tag = f"[{controller.value}] " if controller else ""
        log(f"{tag}ATC (tx): \"{text}\"")

    def transcribe(pcm: bytes, who: str, duration: float,
                   controller: Controller) -> str:
        """STT one transmission. Runs in the controller's worker thread."""
        peak = peak_dbfs(pcm)
        wav_path = save_wav(pcm)
        started = time.monotonic()
        segments, _ = stt.transcribe(
            resample_16k(pcm),
            language="en",
            beam_size=1,
            vad_filter=True,
            initial_prompt="Kutaisi Tower, Colt 1, request taxi to startup, "
                           "ready for departure, cleared to land, cleared for takeoff, "
                           "hold short, inbound, final, runway 25.",
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        latency = (time.monotonic() - started) * 1000
        wav_note = f", wav {wav_path.name}" if wav_path else ""
        if not text:
            log(f"[{controller.value}] {who}: <unclear>  "
                f"({duration:.1f} s, peak {peak:.0f} dBFS{wav_note})")
            return ""
        log(f"[{controller.value}] {who}: \"{text}\"  (stt {latency:.0f} ms, "
            f"{duration:.1f} s, peak {peak:.0f} dBFS{wav_note})")
        return text

    def track_for(who: str):
        """Live CTR track for the transmitting aircraft, or None."""
        if state is None:
            return None
        try:
            for ac in state.aircraft():
                if ac.player.lower() == who.lower():
                    return tracker.track(ac.callsign, ac.lat, ac.lon, ac.alt_ft,
                                         ac.heading)
        except (OSError, RuntimeError) as error:
            log(f"state bridge unavailable: {error}")
        return None

    def traffic() -> list:
        """Every live unit (players + AI), for sequencing clearances."""
        if state is None:
            return []
        try:
            return state.all_units()
        except (OSError, RuntimeError):
            return []

    # One worker thread per controller frequency, sharing the brain + tracker.
    # The lock guards brain/tracker so two controllers can't interleave a
    # read-modify-write on the same pilot's state.
    lock = threading.RLock()
    shared = SharedState(brain=brain, lock=lock, track_for=track_for,
                         transcribe=transcribe, speak=speak, log=log,
                         debug=debug, traffic=traffic)
    workers = {hz: ControllerWorker(controller, hz, shared,
                                    voice=voice_for[controller])
               for hz, controller in controller_by_freq.items()}

    # Optional live map view (Leaflet) showing aircraft + their flight phase.
    if args.map_port:
        from map_server import start_map_server
        start_map_server(airfield, brain, state, args.map_port,
                         host=args.map_host, lock=lock, log=log)

    def on_end(freq: float, who: str, pcm: bytes, duration: float) -> None:
        """SRS rx callback: enqueue to the right controller worker (never blocks)."""
        worker = workers.get(round(freq))
        if worker is None or duration < MIN_TRANSMISSION_SECONDS:
            return
        worker.submit(who, apply_gain(pcm), duration)

    def monitor_ctr() -> None:
        """Poll live positions: warn on unannounced CTR entry, and issue
        go-arounds when the runway is occupied on final."""
        while True:
            time.sleep(2.0)
            if state is None:
                continue
            try:
                players = state.aircraft()
                all_units = state.all_units()
                for ac in players:
                    # Address the pilot by their flight callsign (learned from
                    # their transmissions), not the raw DCS unit name.
                    with lock:
                        callsign = brain.callsign_for_speaker(ac.player)
                        event, tr = tracker.update(callsign, ac.lat, ac.lon,
                                                   ac.alt_ft, ac.heading)
                        warning = brain.on_ctr_event(callsign, event, tr)
                    if warning:
                        log(f"CTR {event.value}: {callsign} ({ac.player})")
                        speak(warning)
                    on_final = airfield.is_on_final(ac.lat, ac.lon, ac.heading)
                    if on_final:
                        occupied = airfield.runway_occupied(
                            all_units, exclude=ac.callsign)
                        with lock:
                            call = brain.check_final(callsign, on_final, occupied)
                        if call:
                            log(f"RUNWAY OCCUPIED: {callsign} ({ac.player})")
                            speak(call)
                    else:
                        with lock:
                            brain.check_final(callsign, False, False)
            except (OSError, RuntimeError):
                pass  # bridge down or mission not running; retry next tick

    def refresh_weather() -> "AtisReport | None":
        """Fetch live weather, update the active runway, return the ATIS report."""
        try:
            weather = state.weather()
        except (OSError, RuntimeError) as error:
            log(f"weather unavailable: {error}")
            return None
        report = build_atis(airfield, weather)
        brain.set_runway(report.active_runway)
        # Keep the airfield's active runway in sync too: the go-around, final
        # and runway-occupancy checks all read it, so they must follow the wind
        # (07 vs 25) just like the clearances do.
        airfield.set_active_runway(report.active_runway)
        brain.set_wind(report.wind_dir, report.wind_speed)
        brain.set_qnh(report.qnh_inhg)
        return report

    def atis_loop() -> None:
        """Broadcast ATIS on its own frequency at a fixed interval."""
        last_letter = None
        while True:
            report = refresh_weather()
            if report is not None:
                if report.information != last_letter:
                    log(f"ATIS {report.information}: runway {report.active_runway}, "
                        f"QNH {report.qnh_inhg:.2f}")
                    last_letter = report.information
                speak(report.broadcast(), atis_hz, atis_voice)
            time.sleep(args.atis_interval)

    def weather_loop() -> None:
        """Keep the active runway in sync with the wind (no ATIS broadcast)."""
        while True:
            refresh_weather()
            time.sleep(args.atis_interval)

    freqs = [freq_hz]
    if ground_hz:
        freqs.append(ground_hz)
    if control_hz:
        freqs.append(control_hz)
    if atis_hz:
        freqs.append(atis_hz)
    client = SrsClient(args.host, args.port, name, freqs,
                       eam_password=args.eam, coalition=2)
    client.on_transmission_end = on_end
    client.start()
    for worker in workers.values():
        worker.start()
    if state is not None:
        initial = refresh_weather()
        if initial is not None:
            log(f"Active runway {initial.active_runway} (wind "
                f"{initial.wind_dir:03.0f}/{initial.wind_speed:.0f} m/s)")
        threading.Thread(target=monitor_ctr, daemon=True).start()
        if atis_hz and args.atis_interval > 0:
            threading.Thread(target=atis_loop, daemon=True).start()
        elif args.atis_interval > 0:
            threading.Thread(target=weather_loop, daemon=True).start()
    log(f"ATC bot listening on {freq_mhz:.3f} MHz AM @ {args.host}:{args.port} "
        f"(log: {log_path.resolve()})")
    for hz, controller in sorted(controller_by_freq.items()):
        log(f"  {controller.value:8s} {hz / 1e6:.3f} MHz  voice "
            f"{voice_names[controller]}")
    if atis_hz:
        log(f"ATIS broadcasting on {atis_mhz:.3f} MHz AM every "
            f"{args.atis_interval:.0f}s  voice {args.atis_voice}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
        print("[*] stopped")


if __name__ == "__main__":
    main()
