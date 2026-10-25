"""ATC trainer: listen on SRS, STT, rules-based reply, TTS, transmit back.

Frequency, tower name and active runway come from airspace.json (per airfield);
CLI flags override them.

Usage:
    uv run atc_bot.py [--airfield Kutaisi] [--airspace airspace.json]
                      [--host IP] [--port 5002] [--freq 270.0] [--name ATC]
                      [--eam atc] [--stt-model small.en] [--voice en_US-amy-medium]
                      [--ground-voice en_US-ryan-medium]
                      [--control-voice en_US-lessac-medium]
                      [--stingray-voice en_US-joe-medium]
                      [--atis-voice en_GB-alan-medium]
                      [--log /tmp/atc_log.txt] [--gain 1.0] [--tx-volume 0.6]
                      [--save-audio]
                      [--state-host 127.0.0.1] [--state-port 10309] [--no-state]
"""

import argparse
import datetime
import math
import os
import re
import threading
import time
import wave
from pathlib import Path

import av
import numpy as np

from airspace import Airspace, bearing_distance
from atis import build_atis
from brain import AtcBrain, Controller, Phraseology
from callsigns import CallsignRegistry
from ctr import CtrEvent, CtrTracker
from phonetics import stt_hint
from srs_client import SrsClient
from speech import is_probably_non_speech, spell_atc_numbers
from state_client import StateClient, norm_identity, resolve_player_unit, slot_callsign
import topgun
from workers import ControllerWorker, SharedState

SAMPLE_RATE = 48000
STT_RATE = 16000
MIN_TRANSMISSION_SECONDS = 0.3
# A landing handoff (Tower -> Ground) waits until the aircraft is on the ground
# AND slow enough to be a rollout, so a touch-and-go (still fast) is not pushed
# to Ground mid-runway. 80 kt gives margin above the DCS airspeed gauge floor
# (~58 kt) while staying well below a touch-and-go speed (~120+ kt).
LANDING_HANDOFF_MAX_KT = 80.0
# ...and low enough to actually be on the ground. `runway_occupied` accepts
# anything below 500 ft AGL, so without this a low, slow aircraft crossing the
# threshold was pushed to Ground while still airborne.
LANDING_HANDOFF_MAX_AGL_FT = 50.0


def is_landing_rollout(on_runway: bool, speed_kt: float,
                       agl_ft: float = 0.0) -> bool:
    """True if the aircraft is on the runway and slow enough to be a rollout.

    Used to gate the Tower->Ground handoff: a touch-and-go is still fast, so it
    stays with Tower instead of being pushed to Ground mid-runway.
    """
    return (bool(on_runway) and speed_kt <= LANDING_HANDOFF_MAX_KT
            and agl_ft <= LANDING_HANDOFF_MAX_AGL_FT)


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


def scale_pcm(pcm: bytes, factor: float) -> bytes:
    """Scale 16-bit mono PCM by `factor`, clipped to int16. 1.0 = unchanged.

    Used for the output volume (`--tx-volume`) so loud Piper voices can be
    quietened without editing the voice files, and for the input gain
    (`--gain`) on received audio.
    """
    if factor == 1.0:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
    return np.clip(samples * factor, -32768, 32767).astype(np.int16).tobytes()


def cuda_available() -> bool:
    """True if a CUDA GPU is usable by CTranslate2 (faster-whisper's backend)."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - any failure means "no GPU"
        return False


def preload_cuda_libs() -> None:
    """Preload cuBLAS/cuDNN from the `nvidia-*` pip packages, if present.

    CTranslate2 loads these lazily at inference time; if they are only in the
    pip package dirs (not on the loader path) the load fails with "Library
    libcublas.so.12 is not found". Loading them here with RTLD_GLOBAL makes them
    available. A no-op when the packages are absent (CPU-only installs).
    """
    import ctypes
    import glob
    import os
    try:
        import nvidia
    except ImportError:
        return
    # `nvidia` is a namespace package (no __file__); use its search path(s).
    bases = list(getattr(nvidia, "__path__", []) or [])
    for base in bases:
        for pattern in ("cublas/lib/libcublas*.so*", "cudnn/lib/libcudnn*.so*"):
            for path in sorted(glob.glob(os.path.join(base, pattern))):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def resolve_stt(model: str | None, device: str, compute: str) -> tuple[str, str, str]:
    """Pick (model, device, compute_type) for Whisper.

    `auto` device uses CUDA when a GPU is available, else CPU. `auto` compute is
    float16 on GPU, int8 on CPU. `auto` model is large-v3 on GPU (accurate
    and fast there) and medium.en on CPU (accurate enough, runs on ~4 cores).
    An explicit value for any of the three always wins.
    """
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    if model is None:
        model = "large-v3" if device == "cuda" else "medium.en"
    return model, device, compute


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
    parser.add_argument("--stingray-freq", type=float, default=None,
                        help="Stingray/AWACS MHz, AM (default: from airspace.json; 0 disables)")
    parser.add_argument("--atis-interval", type=float, default=60.0,
                        help="seconds between ATIS broadcasts (0 disables)")
    parser.add_argument("--name", default=None,
                        help="SRS client name (default: airfield tower name)")
    parser.add_argument("--eam", default=None, help="External AWACS Mode password")
    parser.add_argument("--stt-model", default=None,
                        help="Whisper model (default: auto — large-v3 on "
                             "GPU, medium.en on CPU)")
    parser.add_argument("--stt-device", default="auto",
                        choices=["auto", "cpu", "cuda"],
                        help="STT device (default: auto — cuda if available)")
    parser.add_argument("--stt-compute", default="auto",
                        help="STT compute type (default: auto — float16 on GPU, "
                             "int8 on CPU)")
    parser.add_argument("--voice", default="en_US-amy-medium",
                        help="Piper voice for Tower (default: en_US-amy-medium)")
    parser.add_argument("--ground-voice", default="en_US-ryan-medium",
                        help="Piper voice for Ground (default: en_US-ryan-medium)")
    parser.add_argument("--control-voice", default="en_US-lessac-medium",
                        help="Piper voice for Control (default: en_US-lessac-medium)")
    parser.add_argument("--stingray-voice", default="en_US-joe-medium",
                        help="Piper voice for Stingray/AWACS (default: en_US-joe-medium)")
    parser.add_argument("--atis-voice", default="en_GB-alan-medium",
                        help="Piper voice for ATIS (default: en_GB-alan-medium)")
    parser.add_argument("--log", default="/tmp/atc_log.txt")
    parser.add_argument("--audio-dir", default="/tmp/atc_audio")
    parser.add_argument("--save-audio", action="store_true",
                        help="save received/transmitted audio as WAVs for "
                             "debugging (off by default)")
    parser.add_argument("--keep", type=int, default=10,
                        help="how many rx WAVs to keep with --save-audio")
    parser.add_argument("--gain", type=float, default=1.0,
                        help="input gain for received audio (STT), 1.0 = none")
    parser.add_argument("--tx-volume", type=float, default=0.6,
                        help="output volume for the ATC voices (TTS), 1.0 = "
                             "unchanged; e.g. 0.6 to quieten loud voices")
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
    parser.add_argument("--control-file", default=None,
                        help="state file the map writes the requested airfield "
                             "to before exiting for a restart (default: "
                             "atc/.control). Read by run_server.sh.")
    parser.add_argument("--tracks-file", default=None,
                        help="save/restore flight trails here for debrief. "
                             "Defaults to atc/debrief/tracks_<field>_<time>.json.")
    parser.add_argument("--no-debrief", action="store_true",
                        help="do not save a debrief (trail + comms) for this "
                             "session (saved by default)")
    parser.add_argument("--replay", default=None, metavar="FILE",
                        help="review a saved debrief file on the map, read-only "
                             "(implies loading it as the tracks file)")
    parser.add_argument("--no-announce", action="store_true",
                        help="skip the one-time 'ATC online' announcement")
    parser.add_argument("--debug", action="store_true",
                        help="log routing/state detail for every transmission, "
                             "and force the in-game comms mirror on for all "
                             "pilots (pilots can also toggle it themselves "
                             "with 'trainer, echo on')")
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
    stingray_mhz = args.stingray_freq if args.stingray_freq is not None else airfield.stingray_frequency_mhz

    freq_hz = round(freq_mhz * 1_000_000)
    atis_hz = round(atis_mhz * 1_000_000) if atis_mhz else None
    ground_hz = round(ground_mhz * 1_000_000) if ground_mhz else None
    control_hz = round(control_mhz * 1_000_000) if control_mhz else None
    stingray_hz = round(stingray_mhz * 1_000_000) if stingray_mhz else None
    # frequency (Hz) -> controller role, for routing incoming transmissions
    controller_by_freq = {freq_hz: Controller.TOWER}
    if ground_hz:
        controller_by_freq[ground_hz] = Controller.GROUND
    if control_hz:
        controller_by_freq[control_hz] = Controller.CONTROL
    if stingray_hz:
        controller_by_freq[stingray_hz] = Controller.STINGRAY
    log_path = Path(args.log)
    audio_dir = Path(args.audio_dir)
    save_audio = args.save_audio  # capture rx/tx WAVs for debugging (opt-in)
    if save_audio:
        audio_dir.mkdir(parents=True, exist_ok=True)

    stt_model, stt_device, stt_compute = resolve_stt(
        args.stt_model, args.stt_device, args.stt_compute)
    if stt_device == "cuda":
        preload_cuda_libs()
    print(f"[*] Loading Whisper '{stt_model}' on {stt_device} ({stt_compute})...")
    stt = WhisperModel(stt_model, device=stt_device, compute_type=stt_compute)
    # A distinct voice per controller adds immersion: Tower, Ground, Control and
    # ATIS each sound like a different controller.
    voice_names = {
        Controller.TOWER: args.voice,
        Controller.GROUND: args.ground_voice,
        Controller.CONTROL: args.control_voice,
        Controller.STINGRAY: args.stingray_voice,
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
                     stingray=airfield.stingray,
                     gates=list(airfield.gates), gate_locator=airfield.nearest_gate,
                     airfield=airfield)
    tracker = CtrTracker(airfield)
    # No runway in this banner: the active runway is only known after the first
    # weather refresh, which logs "Active runway … (wind …)" below.
    print(f"[*] Airfield {airfield.name}: {airfield.tower}, {freq_mhz:.3f} MHz, "
          f"CTR ceiling {airfield.ctr.ceiling_ft_agl:.0f} ft AGL")
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
        return scale_pcm(pcm, args.gain)

    def peak_dbfs(pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if not samples.size:
            return -120.0
        peak = float(np.abs(samples).max()) / 32768.0
        return 20 * math.log10(max(peak, 1e-6))

    def save_wav(pcm: bytes) -> Path | None:
        if not save_audio:
            return None
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

    # Piper synthesis is CPU-heavy and shared, so serialise it. Transmission,
    # however, is per agency: each controller frequency has its own SRS client
    # (see `clients`), so a long ATIS on one frequency can never block a Tower
    # reply on another — they are independent clients with independent TX queues.
    tts_lock = threading.Lock()

    def client_for(controller: Controller | None, freq: int | None = None):
        """The SRS client to transmit on for a controller role/frequency.

        ATIS goes out on its own client (keyed by frequency). Everything else
        uses the agency's client, falling back to Tower when a role has no own
        client (e.g. it was disabled).
        """
        if freq is not None and atis_hz and freq == atis_hz and atis_client:
            return atis_client
        if controller is not None:
            c = clients.get(controller)
            if c is not None:
                return c
        return clients[Controller.TOWER]

    def speak(text: str, freq: int = freq_hz, voice=None,
              controller: Controller | None = None,
              agency: str | None = None, log_chatter: bool = True,
              callsign: str = "") -> None:
        """TTS the reply in the given voice and transmit it on the frequency.

        `log_chatter=False` transmits but does not record the line in the log /
        map chatter — used for a repeated ATIS broadcast (identical every
        interval), so the log only shows the first broadcast and each change of
        the information letter.
        """
        # TopGun easter egg: if the reply is the canonical denial, play the
        # drop-in clip instead of synthesising the line (its presence is the
        # switch; see topgun.enabled()).
        if topgun_neg_clip is not None and text == topgun.NEGATIVE_LINE:
            # Pin the film line to the pilot so spawn debrief keeps it
            # (`_filter_chatter` matches the extracted callsign, not "tower").
            pin = f"{callsign}, {text}" if callsign else text
            play_wav(topgun_neg_clip, freq, controller, text=pin)
            return
        spoken = text
        for word, phonetic in PRONUNCIATION.items():
            spoken = spoken.replace(word, phonetic)
        # Read ATC numbers digit-by-digit (headings, wind, runway, QNH) and drop
        # the callsign dash ("Colt 1-1" -> "Colt one one"). Synthesis-only: the
        # log/chatter below still show the real text.
        spoken = spell_atc_numbers(spoken)
        # Default to the voice of the controller we are speaking as, so a call
        # that passes `controller=` but no explicit `voice=` still uses the
        # right voice (e.g. Control's radar climb/descent, not Tower's).
        if voice is None:
            voice = voice_for.get(controller, voice_for[Controller.TOWER])
        with tts_lock:
            syn_config = SynthesisConfig(length_scale=args.speech_rate)
            chunks = list(voice.synthesize(spoken, syn_config=syn_config))
            rate = chunks[0].sample_rate
            pcm22k = b"".join(c.audio_int16_bytes for c in chunks)
            pcm48k = resample_to_48k(pcm22k, rate)
            # Apply the output volume (--tx-volume, or the runtime "trainer,
            # volume X" override) so loud Piper voices can be quietened without
            # touching the voice files.
            volume = brain.tx_volume if brain.tx_volume is not None else args.tx_volume
            pcm48k = scale_pcm(pcm48k, volume)
            # Transmit outside the synthesis lock is not needed (the lock only
            # serialises Piper); the per-agency client queues the audio.
            client_for(controller, freq).transmit(pcm48k, freq)
            if save_audio:
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                with wave.open(str(audio_dir / f"tx_{stamp}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    wav.writeframes(pcm48k)
        tag = f"[{controller.value}] " if controller else ""
        log(f"{tag}ATC (tx): \"{text}\"")
        if log_chatter:
            # TopGun denial has no callsign in the film line; prefix so spawn
            # debrief keeps the clip (filter matches extracted callsign).
            chatter_text = text
            if text == topgun.NEGATIVE_LINE and callsign:
                chatter_text = f"{callsign}, {text}"
            chatter("tx", freq, agency or (controller.value if controller else "atc"),
                    chatter_text, agency or controller)

    def freq_for(controller: Controller) -> float:
        """Frequency (Hz) for a controller role, for the chatter log."""
        for hz, ctrl in controller_by_freq.items():
            if ctrl == controller:
                return hz
        return freq_hz

    def working_controller(callsign: str) -> Controller:
        """Last agency this pilot talked to; Tower if they have never checked in.

        Unsolicited calls (CTR warning, go-around, TopGun sting) must go on the
        frequency the pilot is actually on — this trainer has no Guard, and a
        Tower-only call never reaches someone still on Control.
        """
        with lock:
            return brain.working_controller(callsign)

    def speak_to_pilot(callsign: str, text: str) -> None:
        """Unsolicited TX on the pilot's working frequency."""
        ctrl = working_controller(callsign)
        speak(text, freq=freq_for(ctrl), controller=ctrl)

    def play_wav(path, freq: int = freq_hz,
                 controller: Controller | None = None,
                 text: str = "") -> None:
        """Transmit a drop-in WAV clip (TopGun easter egg). Serialised with TTS
        so the clip and a spoken reply can never overlap on the frequency.

        `text` is the film line recorded in the debrief chatter so the map
        shows that the clip played (the bot log already has `tx wav`).
        """
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        if channels != 1:
            raise ValueError(f"{path}: expected mono, got {channels} channels")
        pcm48k = frames if rate == SAMPLE_RATE else resample_to_48k(frames, rate)
        with tts_lock:
            client_for(controller, freq).transmit(pcm48k, freq)
        tag = f"[{controller.value}] " if controller else ""
        log(f"{tag}ATC (tx wav): {Path(path).name}")
        line = text or Path(path).name
        chatter("tx", freq, controller.value if controller else "atc",
                line, controller)

    def transcribe(pcm: bytes, who: str, duration: float,
                   controller: Controller) -> str:
        """STT one transmission. Runs in the controller's worker thread."""
        peak = peak_dbfs(pcm)
        wav_path = save_wav(pcm)
        started = time.monotonic()
        with lock:
            hint_names = brain.stt_flight_names()
        segments, _ = stt.transcribe(
            resample_16k(pcm),
            language="en",
            # Beam search (not greedy): on GPU we have the headroom, and a wider
            # beam recovers more on noisy radio audio. 1 on CPU to stay fast.
            beam_size=1 if stt_device == "cpu" else 5,
            vad_filter=True,
            # Don't let one segment's text bias the next: on short/noisy clips
            # this causes repeat-hallucination loops.
            condition_on_previous_text=False,
            # Bias the decoder toward the callsigns currently online (and their
            # known STT errors), not the whole mission list — 60 flights blow
            # the 900-char cap and drown the one name the pilot is about to say.
            #
            # No `initial_prompt`: it becomes the decoder *prefix*, so on a
            # near-silent clip (a PTT click or breath) Whisper echoes it back
            # verbatim as if the pilot had said it. `hotwords` biases the same
            # vocabulary without that failure mode.
            hotwords=stt_hint(hint_names,
                              [airfield.name, airfield.tower]),
        )
        segments = list(segments)  # materialise: the guard below re-reads them
        text = " ".join(s.text.strip() for s in segments).strip()
        # Drop a transcript the model itself flagged as non-speech (a PTT click
        # or breath can decode to a hallucinated phrase).
        if is_probably_non_speech(segments):
            text = ""
        latency = (time.monotonic() - started) * 1000
        wav_note = f", wav {wav_path.name}" if wav_path else ""
        if not text:
            log(f"[{controller.value}] {who}: <unclear>  "
                f"({duration:.1f} s, peak {peak:.0f} dBFS{wav_note})")
            chatter("rx", freq_for(controller), who, "<unclear>", controller)
            return ""
        log(f"[{controller.value}] {who}: \"{text}\"  (stt {latency:.0f} ms, "
            f"{duration:.1f} s, peak {peak:.0f} dBFS{wav_note})")
        chatter("rx", freq_for(controller), who, text, controller)
        return text

    warned_ambiguous = set()  # speakers already warned about multiple matches

    def unit_for_speaker(who: str):
        """The live player unit whose identity matches an SRS speaker name.

        The SRS name and the DCS player name need not be the same, and the TTI
        mission has many slots sharing one callsign (several "Springfield11"
        aircraft). `state_client.resolve_player_unit` tries player-name, then the
        callsign the brain learned for this speaker, then the flight callsign
        the SRS name implies, and only then (solo) the lone online pilot — and
        returns None on ambiguity, so radar never binds to the wrong aircraft.
        """
        if state is None or not who:
            return None
        try:
            units = state.aircraft()
        except (OSError, RuntimeError) as error:
            log(f"state bridge unavailable: {error}")
            return None
        with lock:
            learned = brain.callsign_for_speaker(who)
            if learned == who:
                learned = ""  # not learned yet
            solo = len(units) == 1
            # Prefer locating by SRS position when the mission reports it (and
            # it is non-zero), to disambiguate players who share a name.
            pos = None
            if client is not None:
                try:
                    p = client.client_position(who)
                    if p and (abs(p[0]) > 1e-6 or abs(p[1]) > 1e-6):
                        pos = p
                except Exception:
                    pos = None
            unit = resolve_player_unit(units, who, learned, solo=solo,
                                       speaker_pos=pos)
        if unit is None and units and _ambiguity_would_help(who, units):
            key = norm_identity(who)
            if key not in warned_ambiguous:
                warned_ambiguous.add(key)
                log(f"radar: could not uniquely resolve speaker {who!r} to a "
                    f"live unit ({len(units)} player units online)")
        return unit

    def _ambiguity_would_help(who: str, units) -> bool:
        """True if resolving `who` failed only because of a duplicate identity."""
        key = norm_identity(who)
        matches = [u for u in units if norm_identity(u.player) == key
                   or norm_identity(u.callsign) == key]
        return len(matches) > 1

    def track_for(who: str):
        """Live CTR track for the transmitting aircraft, or None."""
        ac = unit_for_speaker(who)
        if ac is None:
            return None
        return tracker.track(ac.callsign, ac.lat, ac.lon, ac.alt_ft, ac.heading)

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
    # With --debug, fly the trails to atc/debrief/tracks_<field>_<time>.json (or
    # --tracks-file) so a sortie can be replayed for after-action review after
    # the bot stops. --replay loads a saved file read-only.
    map_service = None
    if args.map_port:
        from map_server import (start_map_server, ControlPlane, DEBRIEF_DIR,
                                RESTART_EXIT_CODE)
        tracks_file = args.replay or args.tracks_file
        debrief_dir = None
        if tracks_file is None and not args.no_debrief:
            # One named snapshot per pilot spawn:
            # atc/debrief/tracks_<field>_<player>_<callsign>_<time>.json
            # Saved by default (not only with --debug); --no-debrief opts out.
            DEBRIEF_DIR.mkdir(parents=True, exist_ok=True)
            debrief_dir = DEBRIEF_DIR

        # Web-driven restart / airfield switch. The map server runs inside this
        # process, so it cannot restart the bot itself: it writes the requested
        # airfield to a small state file and asks us to exit with a sentinel
        # code. The outer run_server.sh loop reads the file and relaunches the
        # bot with the new --airfield. Airfields are validated against the
        # loaded config (an allowlist), so the endpoint can never launch an
        # arbitrary --airfield.
        control_file = (Path(args.control_file) if args.control_file
                        else Path(__file__).with_name(".control"))

        def on_control_exit(action: str, new_airfield: str, save: bool) -> None:
            # Persist the debrief before leaving, so a web-driven restart does
            # not lose the sortie trail.
            if map_service is not None:
                map_service.save_tracks()
            if action == "restart":
                print(f"[*] Restart requested -> airfield {new_airfield} "
                      f"(exit {RESTART_EXIT_CODE})")
                os._exit(RESTART_EXIT_CODE)
            print("[*] Stop requested via the map.")
            os._exit(0)

        control = ControlPlane(airspace.names(), control_file,
                               on_control_exit, airfield.name, debug=args.debug)
        _server, map_service = start_map_server(
            airfield, brain, state, args.map_port,
            host=args.map_host, lock=lock, log=log, tracks_file=tracks_file,
            replay=bool(args.replay), control=control, debrief_dir=debrief_dir)

    # Last speaker to transmit, so a TX mirror can be addressed to them.
    _debug_reply_to = [""]

    def chatter(kind: str, freq_hz: float, who: str, text: str,
                controller: Controller | str | None = None) -> None:
        """Push one radio event to the map's chatter log (if the map is on)."""
        if map_service is not None:
            if isinstance(controller, Controller):
                agency = controller.value
            else:
                agency = controller or ""
            map_service.log_chatter(kind, freq_hz / 1e6, who, text, agency)
        # In-game comms mirror: echo the exchange into the pilot's DCS message
        # feed, so a trainer can read what was heard (RX) and answered (TX)
        # without the map. Per-pilot: enabled by the pilot saying "trainer, logs
        # on" (any frequency), or forced on for everyone with --debug. Read-only
        # side channel: it never feeds the brain.
        if state is not None:
            if kind == "rx":
                _debug_reply_to[0] = who
                if args.debug or _mirror_for(who):
                    _mirror(f"RX {who}: {text}", who)
            elif args.debug or _mirror_for(_debug_reply_to[0]):
                _mirror(f"TX {who}: {text}", _debug_reply_to[0])

    def _mirror_for(who: str) -> bool:
        """True if this speaker asked for the in-game comms mirror."""
        if not who:
            return False
        with lock:
            callsign = brain.callsign_for_speaker(who)
            return brain.mirror_enabled(callsign)

    def _mirror(line: str, player: str) -> None:
        # The bridge targets a player by their DCS player name, but `player` here
        # is the SRS name / callsign, which can differ (TTI shares many slots per
        # callsign). Resolve to the live DCS unit's player name so the mirror is
        # addressed to the pilot; if we can't, the bridge falls back to a
        # mission-wide outText (still shows, just to everyone).
        name = player
        ac = unit_for_speaker(player)
        if ac is not None and ac.player:
            name = ac.player
        try:
            state.message(line, player=name)
        except (OSError, RuntimeError):
            pass  # bridge down / no message API; the debug log is best-effort

    # Wording coach (coach mode): a coaching note is a direct in-game message
    # to the pilot who made the call — never spoken on the radio, so the
    # frequency stays clean for everyone else. Logged to the map chatter too
    # (kind "coach") so a debrief shows what was taught.
    def coach_note(who: str, note: str) -> None:
        _mirror(f"COACH: {note}", who)
        if map_service is not None:
            map_service.log_chatter("coach", 0.0, who, note, "coach")

    shared.coach = coach_note


    # Position snapshots at key-up: the pilot reads their instruments while
    # speaking, so the state that matches what they SAYS is the track at
    # transmission START — not at parse time, where seconds of Whisper STT
    # have already moved the aircraft past the reported position.
    _start_tracks: dict[str, object] = {}
    start_tracks_lock = threading.Lock()

    def on_start(freq: float, who: str) -> None:
        try:
            track = track_for(who)
        except Exception:
            track = None
        with start_tracks_lock:
            if track is not None:
                _start_tracks[who] = track

    def on_end(freq: float, who: str, pcm: bytes, duration: float) -> None:
        """SRS rx callback: enqueue to the right controller worker (never blocks)."""
        with start_tracks_lock:
            start_track = _start_tracks.pop(who, None)
        worker = workers.get(round(freq))
        if worker is None or duration < MIN_TRANSMISSION_SECONDS:
            return
        worker.submit(who, apply_gain(pcm), duration, start_track)

    # TopGun easter egg: the tower position (from the bridge, cached) and the
    # per-callsign bust timer (close + low + fast held for BUST_HOLD_S).
    _tower_pos: list = [None]
    _bust_watch = topgun.BustWatch()

    def tower_position():
        """(lat, lon) of the tower for this airfield, or None."""
        if airfield.tower_pos:
            return airfield.tower_pos
        if _tower_pos[0] is None and state is not None:
            try:
                info = state.tower(airfield.name)
            except (OSError, RuntimeError):
                info = None
            if info:
                lat, lon = info["lat"], info["lon"]
                if airfield.ctr.distance_nm(lat, lon) <= 3.0:
                    _tower_pos[0] = (lat, lon)
        return _tower_pos[0]

    topgun_on = topgun.enabled()
    topgun_neg_clip = topgun.negative_clip()
    topgun_bust_clip = topgun.clip("topgun_bust")
    if topgun_on:
        log(f"TopGun easter egg ENABLED ("
            f"negative {'wav' if topgun_neg_clip else 'tts'}, "
            f"bust {'wav' if topgun_bust_clip else 'tts'}).")

    def monitor_ctr() -> None:
        """Poll live positions: warn on unannounced CTR entry, and issue
        go-arounds when the runway is occupied on final."""
        greeted: set[str] = set()  # players already sent the radio setup
        seen_players: set[str] = set()
        seen_units: dict[str, str] = {}
        while True:
            time.sleep(2.0)
            if state is None:
                continue
            try:
                players = state.aircraft()
                all_units = state.all_units()
                with lock:
                    # Keep the callsign registry and speaker map in step with
                    # who is actually flying: learn dynamic-slot callsigns, prune
                    # departed players, and seed "player -> callsign" for the
                    # units whose name *is* a callsign (DCS reports only the
                    # name, so a synthetic one is deliberately not seeded).
                    brain.sync_speakers(
                        [(ac.player, ac.callsign) for ac in players])
                    # Solo training mode: with one (or zero) players online, an
                    # unrecognized call is prompted instead of ignored.
                    brain.set_pilot_count(len(players))
                    # Known callsigns online, used to attribute a garbled call to
                    # a pilot whose flight name the transmission still resembles.
                    # Only pilots we can actually name are included: a raw unit
                    # name ("Kutaisi_F/A-18C Lot 20_2-") could only mislead it.
                    brain.set_active_pilots(
                        brain.known_callsigns([ac.player for ac in players]))
                online = {ac.player for ac in players if ac.player}
                for name in list(seen_players - online):
                    log(f"PLAYER LEFT: {name}")
                    seen_players.discard(name)
                    seen_units.pop(name, None)
                    if map_service is not None:
                        map_service.end_spawn(name)
                for ac in players:
                    if not ac.player:
                        continue
                    prev_unit = seen_units.get(ac.player)
                    respawned = (prev_unit is not None
                                 and prev_unit != ac.callsign)
                    if ac.player in seen_players and not respawned:
                        continue
                    seeded = slot_callsign(ac.callsign) or "-"
                    with lock:
                        known = brain.callsign_for_speaker(ac.player)
                    callsign = "" if known == ac.player else known
                    if respawned:
                        log(f"PLAYER RESPAWN: {ac.player}  "
                            f"{prev_unit!r} -> {ac.callsign!r}  "
                            f"slot={seeded}  known={callsign or '-'}")
                        if map_service is not None:
                            map_service.end_spawn(ac.player)
                    else:
                        seen_players.add(ac.player)
                        log(f"PLAYER JOIN: {ac.player}  type={ac.type}  "
                            f"unit={ac.callsign!r}  slot={seeded}  "
                            f"known={callsign or '-'}")
                    seen_units[ac.player] = ac.callsign
                    if map_service is not None:
                        map_service.begin_spawn(
                            ac.player, callsign, unit=ac.callsign)
                # Greet each newly-seen pilot with the radio setup, so they can
                # tune in without reading the docs. Sent once per player (by
                # DCS name) via the in-game message feed. Retry until the bridge
                # confirms delivery: a pilot who joins while the mission is
                # still loading is not yet in net.get_player_list, so the first
                # attempt can silently fall back to a broadcast (or nothing).
                for ac in players:
                    if ac.player and ac.player not in greeted:
                        try:
                            if state.message("\n".join(radio_setup_lines()),
                                             player=ac.player):
                                greeted.add(ac.player)
                        except (OSError, RuntimeError):
                            pass  # bridge down; retry next tick
                # Record the debrief trail whether or not the map page is open,
                # so a completed sortie is saved even with no browser polling.
                if map_service is not None:
                    map_service.record(players)
                for ac in players:
                    # Address the pilot by their flight callsign (learned from
                    # their transmissions), not the raw DCS unit name.
                    with lock:
                        callsign = brain.callsign_for_speaker(ac.player)
                        event, tr = tracker.update(callsign, ac.lat, ac.lon,
                                                   ac.alt_ft, ac.heading,
                                                   getattr(ac, "speed_kt", 0.0))
                        warning = brain.on_ctr_event(callsign, event, tr)
                    if warning:
                        log(f"CTR {event.value}: {callsign} ({ac.player})")
                        speak_to_pilot(callsign, warning)
                    # TopGun easter egg: only for a pilot who asked to bust the
                    # tower, and only once per pass. This is off to the side of
                    # the ATC state machine: it plays a sting and adds nothing to
                    # the clearance log.
                    if topgun_on:
                        with lock:
                            armed = brain.topgun_armed(callsign)
                        if armed:
                            pos = tower_position()
                            if pos is not None:
                                _, tdist = bearing_distance(
                                    ac.lat, ac.lon, pos[0], pos[1])
                                agl = ac.alt_ft - airfield.elevation_ft
                                spd = getattr(ac, "speed_kt", 0.0)
                                # The 2 s poll can skip a 200 m cylinder at
                                # 500 kt; observe() tests the segment from the
                                # last sample, interpolated at closest approach.
                                if tdist <= topgun.BUST_DEBUG_NM:
                                    log(f"TOPGUN: {callsign} {tdist * 1852:.0f}m "
                                        f"{agl:.0f}ft AGL {spd:.0f}kt")
                                if _bust_watch.observe(
                                        callsign, ac.lat, ac.lon, agl, spd,
                                        pos[0], pos[1], time.monotonic()):
                                    with lock:
                                        brain.disarm_topgun(callsign)
                                    log(f"TOPGUN: tower busted by {callsign} "
                                        f"({ac.player}) at {tdist:.2f} nm, "
                                        f"{agl:.0f} ft AGL, {spd:.0f} kt")
                                    ctrl = working_controller(callsign)
                                    pin = f"{callsign}, {topgun.BUST_LINE}"
                                    if topgun_bust_clip is not None:
                                        play_wav(topgun_bust_clip, freq_for(ctrl),
                                                 controller=ctrl, text=pin)
                                    else:
                                        speak(pin, freq=freq_for(ctrl),
                                              controller=ctrl,
                                              callsign=callsign)
                    on_final = airfield.is_on_final(ac.lat, ac.lon, ac.heading)
                    if on_final:
                        occupied = airfield.runway_occupied(
                            all_units, exclude=ac.player)
                        with lock:
                            call = brain.check_final(callsign, on_final,
                                                     occupied, all_units)
                        if call:
                            log(f"RUNWAY OCCUPIED: {callsign} ({ac.player})")
                            speak_to_pilot(callsign, call)
                    else:
                        with lock:
                            brain.check_final(callsign, False, False)
                    # Radar-driven landing clearance: once the arrival has
                    # rolled out of the pattern turn and is established on
                    # final, Tower clears them to land unprompted (the OHB
                    # and pattern calls are status, not a request).
                    with lock:
                        land_call = brain.check_landing_clearance(
                            callsign, tr, all_units, speaker=ac.player)
                    if land_call:
                        log(f"CLEARED TO LAND (radar): {callsign} "
                            f"({ac.player})")
                        speak_to_pilot(callsign, land_call)
                    # Altitude bust: above the CTR ceiling while still under
                    # Tower/Ground control (not handed to Control).
                    with lock:
                        alt_call = brain.check_altitude(callsign, tr)
                    if alt_call:
                        log(f"ALTITUDE BUST: {callsign} ({ac.player})")
                        speak_to_pilot(callsign, alt_call)
                    # Runway incursion: on the runway without a clearance.
                    on_rwy = airfield.runway_occupied(
                        [ac], exclude=None, on_runway=True)
                    with lock:
                        inc_call = brain.check_incursion(callsign, on_rwy)
                    if inc_call:
                        log(f"RUNWAY INCURSION: {callsign} ({ac.player})")
                        speak_to_pilot(callsign, inc_call)
                    # Call back a pilot held for an occupied runway, unprompted,
                    # once it clears (takeoff line-up or landing clearance).
                    busy = airfield.runway_occupied(all_units, exclude=ac.player)
                    with lock:
                        clear_call = brain.check_runway_clear(callsign, busy)
                    if clear_call:
                        log(f"RUNWAY CLEAR: {callsign} ({ac.player})")
                        speak(clear_call, controller=Controller.TOWER)
                    # After landing, Tower pushes the flight to Ground at
                    # touchdown (Master Arms SOP) — the pilot does not have to
                    # report "vacated" to Tower.
                    on_rwy_self = airfield.runway_occupied(
                        [ac], exclude=None, on_runway=True)
                    # Only hand off once the aircraft is actually rolling out
                    # (on the ground and slow): a touch-and-go is still fast, so
                    # it stays with Tower.
                    landed = is_landing_rollout(
                        on_rwy_self, getattr(ac, "speed_kt", 0.0),
                        ac.alt_ft - airfield.elevation_ft)
                    with lock:
                        handoff = brain.check_landing_handoff(callsign, landed)
                    if handoff:
                        log(f"LANDED: {callsign} ({ac.player}) -> Ground")
                        speak(handoff, controller=Controller.TOWER)
                    # Departure handoff: Tower pushes a departure to Control once it is
                    # past the runway (MA SOP: "expect instructions to switch
                    # to control"). The pilot does not have to report "airborne".
                    with lock:
                        dep_handoff = brain.check_departure_handoff(callsign, tr)
                    if dep_handoff:
                        log(f"AIRBORNE: {callsign} ({ac.player}) -> Control")
                        speak(dep_handoff, controller=Controller.TOWER)
                    # Departure climb: a departure that checked in with Control
                    # while still inside the CTR is climbed once it leaves the
                    # zone (Control watches on radar; no report needed).
                    with lock:
                        climb = brain.check_departure_climb(callsign, tr)
                    if climb:
                        log(f"CLIMB: {callsign} ({ac.player})")
                        speak(climb, freq=freq_for(Controller.CONTROL),
                              controller=Controller.CONTROL)
                    # Stingray handoff: once the departure is at Angels 15,
                    # Control pushes them to AWACS on channel 3 (SOP). We do
                    # not simulate Stingray, but the call still goes out.
                    with lock:
                        stingray = brain.check_stingray_handoff(callsign, tr)
                    if stingray:
                        log(f"STINGRAY: {callsign} ({ac.player})")
                        speak(stingray, freq=freq_for(Controller.CONTROL),
                              controller=Controller.CONTROL)
                    # Arrival descent: after the far-out step-down, Control
                    # issues the final "descend to 1500 feet" once the aircraft
                    # reaches the entry area.
                    with lock:
                        descend = brain.check_arrival_descent(callsign, tr)
                    if descend:
                        log(f"DESCEND: {callsign} ({ac.player})")
                        speak(descend, freq=freq_for(Controller.CONTROL),
                              controller=Controller.CONTROL)
                    # Arrival handoff: after the "descend to 1500" readback,
                    # wait until they are actually at 1500 before pushing to
                    # Tower (they may still be at Angels 8).
                    with lock:
                        desc_hand = brain.check_descent_handoff(callsign, tr)
                    if desc_hand:
                        log(f"LEVEL 1500: {callsign} ({ac.player}) -> Tower")
                        speak(desc_hand, freq=freq_for(Controller.CONTROL),
                              controller=Controller.CONTROL)
                    # Takeoff clearance: after the pilot reads back "line up and
                    # wait", wait until they are actually lined up on the runway
                    # before clearing takeoff (they may still be taxiing on).
                    with lock:
                        lineup = brain.check_lineup(callsign, tr)
                    if lineup:
                        log(f"LINED UP: {callsign} ({ac.player})")
                        speak(lineup, controller=Controller.TOWER)
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
        brain.set_weather(report.weather_summary())
        return report

    def atis_loop() -> None:
        """Broadcast ATIS on its own frequency at a fixed interval.

        The broadcast repeats every interval, so it is only *logged* on the
        first broadcast and whenever the information letter changes — otherwise
        the log/chatter would be flooded with identical ATIS lines.
        """
        last_letter = None
        while True:
            report = refresh_weather()
            if report is not None:
                changed = report.information != last_letter
                if changed:
                    log(f"ATIS {report.information}: runway {report.active_runway}, "
                        f"QNH {report.qnh_inhg:.2f}")
                    last_letter = report.information
                speak(report.broadcast(), atis_hz, atis_voice, agency="atis",
                      log_chatter=changed)
            time.sleep(args.atis_interval)

    def weather_loop() -> None:
        """Keep the active runway in sync with the wind (no ATIS broadcast)."""
        while True:
            refresh_weather()
            time.sleep(args.atis_interval)

    # One SRS client per agency, each joined to every ATC frequency but
    # *transmitting* on its own. Because each client has its own TX queue and
    # pacing thread, a long ATIS (or a slow Control reply) can never block a
    # Tower/Ground reply: they are fully independent radio positions.
    #
    # RX fan-in: only ONE client carries the `on_transmission_end` callback (the
    # Tower client), so a transmission heard by several clients is delivered to
    # the workers exactly once. `on_end` routes it to the right agency by the
    # frequency it arrived on, so a single RX client still serves all radios.
    freqs = [freq_hz]
    if ground_hz:
        freqs.append(ground_hz)
    if control_hz:
        freqs.append(control_hz)
    if stingray_hz:
        freqs.append(stingray_hz)
    if atis_hz:
        freqs.append(atis_hz)
    clients: dict[Controller, SrsClient] = {}
    for controller, _hz in [(Controller.TOWER, freq_hz),
                            (Controller.GROUND, ground_hz),
                            (Controller.CONTROL, control_hz),
                            (Controller.STINGRAY, stingray_hz)]:
        if _hz is None and controller is not Controller.TOWER:
            continue
        clients[controller] = SrsClient(
            args.host, args.port, f"{name} {controller.value}", freqs,
            eam_password=args.eam, coalition=2)
    # ATIS needs its own client only so its (long) broadcast never blocks a
    # reply; it is re-listenable and lower priority than live traffic.
    atis_client = None
    if atis_hz:
        atis_client = SrsClient(args.host, args.port, f"{name} atis",
                                [freq_hz] + ([atis_hz] if atis_hz else []),
                                eam_password=args.eam, coalition=2)
    all_clients = list(clients.values()) + ([atis_client] if atis_client else [])
    # Each client must ignore the audio of every OTHER bot client, or it hears
    # the bot's own radio (e.g. the "ATC online" / ATIS broadcasts) and answers
    # it — a runaway echo loop. Only a real pilot's voice should reach the brain.
    own_guids = {c.guid for c in all_clients}
    for c in all_clients:
        c.ignore_guids = set(own_guids)  # ignore every bot client, incl. self
    # Single RX entry point: the Tower client delivers every transmission.
    clients[Controller.TOWER].on_transmission_end = on_end
    clients[Controller.TOWER].on_transmission_start = on_start
    client = clients[Controller.TOWER]  # SRS roster (positions) is shared
    for c in all_clients:
        c.start()
    for worker in workers.values():
        worker.start()
    if state is not None:
        initial = refresh_weather()
        if initial is not None:
            log(f"Active runway {initial.active_runway} (wind "
                f"{initial.wind_dir:03.0f}/{initial.wind_speed:.0f} m/s)")
        # Coalition bullseye from the mission (blue). Fetched once: it is a
        # mission-editor constant, not weather. Without it, a bullseye call is
        # acknowledged but not position-checked.
        try:
            bullseye = state.bullseye()
        except (OSError, RuntimeError) as error:
            bullseye = None
            log(f"bullseye unavailable: {error}")
        if bullseye:
            brain.set_bullseye(bullseye["lat"], bullseye["lon"])
            log(f"Bullseye (blue): {bullseye['lat']:.4f}, {bullseye['lon']:.4f}")
        else:
            log("Bullseye (blue): not set in mission — bullseye calls are "
                "acknowledged without a position check")
        threading.Thread(target=monitor_ctr, daemon=True).start()
        if atis_hz and args.atis_interval > 0:
            threading.Thread(target=atis_loop, daemon=True).start()
        elif args.atis_interval > 0:
            threading.Thread(target=weather_loop, daemon=True).start()
    log(f"ATC bot listening on {freq_mhz:.3f} MHz AM @ {args.host}:{args.port} "
        f"(log: {log_path.resolve()})")

    # Radio setup summary, so a pilot can tune in without reading the docs.
    # Channel numbers come from airspace.json `channels` (per airfield).
    channels = airfield.channels or {}

    def radio_setup_lines() -> list[str]:
        """The radio-setup summary (also sent to pilots who join)."""
        lines = [f"ATC trainer online at {airfield.name} — radio setup:"]
        # Order by channel number (Ground 6, Tower 7, Control 8), not by
        # frequency, so the list reads in the order a pilot tunes them.
        def channel_of(controller) -> int:
            ch = channels.get(controller.value, "")
            return int(ch) if str(ch).isdigit() else 99

        for hz, controller in sorted(controller_by_freq.items(),
                                     key=lambda item: channel_of(item[1])):
            ch = channels.get(controller.value, "")
            ch_note = f"Ch {ch}  " if ch else ""
            lines.append(f"  {controller.value.capitalize():8s} {ch_note}"
                         f"{hz / 1e6:.3f} MHz AM")
        if atis_hz:
            ch = channels.get("atis", "")
            ch_note = f"Ch {ch}  " if ch else ""
            lines.append(f"  {'Atis':8s} {ch_note}{atis_mhz:.3f} MHz AM  "
                         f"(every {args.atis_interval:.0f}s)")
        lines.append('  Say "Trainer, <callsign>, help" on any frequency for a hint.')
        return lines

    for line in radio_setup_lines():
        log(line)

    # One-time "ATC online" announcement on each controller frequency, so a
    # pilot tuning in knows the position is manned. (A trainer convention, not
    # real phraseology — real controllers never broadcast their arrival.)
    if not args.no_announce:
        def announce() -> None:
            time.sleep(2.0)  # let SRS settle before transmitting
            for hz, controller in sorted(controller_by_freq.items()):
                text = brain.phraseology.render(
                    "atc_online", agency=brain._agency(controller))
                speak(text, hz, voice_for[controller], controller)
        threading.Thread(target=announce, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for c in all_clients:
            c.stop()
        print("[*] stopped")


if __name__ == "__main__":
    main()
