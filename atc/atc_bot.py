"""ATC trainer: listen on SRS, STT, rules-based reply, TTS, transmit back.

Usage:
    uv run atc_bot.py [--host IP] [--port 5002] [--freq 251.0] [--name ATC]
                      [--eam atc] [--stt-model small.en] [--voice en_US-amy-medium]
                      [--log /tmp/atc_log.txt] [--keep 10] [--gain 1.0]
"""

import argparse
import datetime
import math
import time
import wave
from pathlib import Path

import av
import numpy as np

from brain import AtcBrain
from srs_client import SrsClient

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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--freq", type=float, default=251.0, help="MHz, AM")
    parser.add_argument("--name", default="Kutaisi Tower")
    parser.add_argument("--eam", default=None, help="External AWACS Mode password")
    parser.add_argument("--stt-model", default="small.en")
    parser.add_argument("--voice", default="en_US-amy-medium")
    parser.add_argument("--log", default="/tmp/atc_log.txt")
    parser.add_argument("--audio-dir", default="/tmp/atc_audio")
    parser.add_argument("--keep", type=int, default=10)
    parser.add_argument("--gain", type=float, default=1.0)
    parser.add_argument("--speech-rate", type=float, default=0.7,
                        help="Piper length_scale; lower = faster (0.6-1.0)")
    args = parser.parse_args()

    from faster_whisper import WhisperModel
    from piper import PiperVoice
    from piper.config import SynthesisConfig

    freq_hz = round(args.freq * 1_000_000)
    log_path = Path(args.log)
    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Loading Whisper '{args.stt_model}'...")
    stt = WhisperModel(args.stt_model, device="cpu", compute_type="int8")
    print(f"[*] Loading Piper voice '{args.voice}'...")
    tts = PiperVoice.load(f"voices/{args.voice}.onnx")
    brain = AtcBrain()
    print("[*] Ready.")

    def log(line: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"[{stamp}] {line}"
        print(text, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

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
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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

    def speak(text: str) -> None:
        """TTS the reply and transmit it on the tower frequency."""
        spoken = text
        for word, phonetic in PRONUNCIATION.items():
            spoken = spoken.replace(word, phonetic)
        syn_config = SynthesisConfig(length_scale=args.speech_rate)
        chunks = list(tts.synthesize(spoken, syn_config=syn_config))
        rate = chunks[0].sample_rate
        pcm22k = b"".join(c.audio_int16_bytes for c in chunks)
        pcm48k = resample_to_48k(pcm22k, rate)
        client.transmit(pcm48k, freq_hz)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        with wave.open(str(audio_dir / f"tx_{stamp}.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm48k)
        log(f"ATC (tx): \"{text}\"")

    def on_end(freq: float, who: str, pcm: bytes, duration: float) -> None:
        if freq != freq_hz or duration < MIN_TRANSMISSION_SECONDS:
            return
        pcm = apply_gain(pcm)
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
            log(f"{who}: <unclear>  ({duration:.1f} s, peak {peak:.0f} dBFS{wav_note})")
            return
        log(f"{who}: \"{text}\"  (stt {latency:.0f} ms, {duration:.1f} s, "
            f"peak {peak:.0f} dBFS{wav_note})")
        reply = brain.handle(text)
        if reply:
            speak(reply)
        else:
            log("ATC: <no matching intent>")

    client = SrsClient(args.host, args.port, args.name, [freq_hz],
                       eam_password=args.eam, coalition=2)
    client.on_transmission_end = on_end
    client.start()
    log(f"ATC bot listening on {args.freq:.3f} MHz AM @ {args.host}:{args.port} "
        f"(log: {log_path.resolve()})")
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
