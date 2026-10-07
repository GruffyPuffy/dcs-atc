"""ATC trainer step 1: join SRS, listen on a frequency, STT transmissions to a log.

Usage:
    uv run listen.py [--host IP] [--port 5002] [--freq 251.0] [--name ATC]
                     [--eam atc] [--model base.en] [--log /tmp/atc_log.txt]
                     [--keep 10] [--gain 1.0]
"""

import argparse
import datetime
import math
import time
import wave
from pathlib import Path

import av
import numpy as np

from srs_client import SrsClient

SAMPLE_RATE = 48000
STT_RATE = 16000  # faster-whisper expects 16 kHz numpy arrays
MIN_TRANSMISSION_SECONDS = 0.3


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def resample_16k(pcm: bytes) -> np.ndarray:
    """48 kHz s16 mono PCM -> 16 kHz float32 for faster-whisper."""
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--freq", type=float, default=251.0, help="MHz, AM")
    parser.add_argument("--name", default="ATC-Bot")
    parser.add_argument("--eam", default=None, help="External AWACS Mode password")
    parser.add_argument("--model", default="small.en")
    parser.add_argument("--log", default="/tmp/atc_log.txt")
    parser.add_argument("--audio-dir", default="/tmp/atc_audio",
                        help="where transmission wavs are kept")
    parser.add_argument("--keep", type=int, default=10,
                        help="keep the last N transmissions as wav (0 = keep all, -1 = none)")
    parser.add_argument("--gain", type=float, default=1.0,
                        help="PCM gain before STT/save (try 2-4 if audio is quiet)")
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    freq_hz = round(args.freq * 1_000_000)
    log_path = Path(args.log)
    audio_dir = Path(args.audio_dir)
    if args.keep >= 0:
        audio_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Loading Whisper model '{args.model}' (first run downloads it)...")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print("[*] Model ready.")

    def log(line: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"[{stamp}] {line}"
        print(text, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def on_start(freq: float, who: str) -> None:
        print(f"[rx] {freq / 1e6:.3f} MHz: {who} is transmitting...", flush=True)

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
        if args.keep < 0:
            return None
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = audio_dir / f"{stamp}_{int((time.time() % 1) * 1000):03d}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        if args.keep > 0:
            wavs = sorted(audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            for old in wavs[:-args.keep]:
                old.unlink(missing_ok=True)
        return path

    def on_end(freq: float, who: str, pcm: bytes, duration: float) -> None:
        if freq != freq_hz or duration < MIN_TRANSMISSION_SECONDS:
            return
        pcm = apply_gain(pcm)
        peak = peak_dbfs(pcm)
        wav_path = save_wav(pcm)
        started = time.monotonic()
        segments, _ = model.transcribe(
            resample_16k(pcm),
            language="en",
            beam_size=1,
            vad_filter=True,
            initial_prompt="Kutaisi Tower, Batumi Tower, Colt 1, Adder, Ford, "
                           "Hawg, inbound, final, runway 25, request taxi to startup, "
                           "cleared to land.",
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        latency = (time.monotonic() - started) * 1000
        wav_note = f", wav {wav_path.name}" if wav_path else ""
        if text:
            log(f"{who}: \"{text}\"  (stt {latency:.0f} ms, {duration:.1f} s, "
                f"peak {peak:.0f} dBFS{wav_note})")
        else:
            log(f"{who}: <unclear>  ({duration:.1f} s, peak {peak:.0f} dBFS{wav_note})")

    client = SrsClient(args.host, args.port, args.name, [freq_hz],
                       eam_password=args.eam, coalition=2)
    client.on_transmission_start = on_start
    client.on_transmission_end = on_end
    client.start()
    log(f"listening on {args.freq:.3f} MHz AM @ {args.host}:{args.port} "
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
