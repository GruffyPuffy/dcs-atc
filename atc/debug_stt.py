"""Offline STT debugging: run Whisper over saved wav files with verbose output.

Usage:
    uv run debug_stt.py samples/20261006_220321_775.wav [more.wav ...]
    uv run debug_stt.py samples/            # all wavs in a directory
"""

import sys
import time
from pathlib import Path

import numpy as np
import wave

from phonetics import stt_hint


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1, f"{path}: expected mono"
        assert wav.getsampwidth() == 2, f"{path}: expected 16-bit"
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, rate


def main() -> None:
    paths = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.wav")))
        else:
            paths.append(p)
    if not paths:
        print("no wav files given")
        return

    from faster_whisper import WhisperModel

    model_name = "base.en"
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    print(f"[*] model: {model_name}\n")

    for path in paths:
        audio, rate = load_wav(path)
        duration = len(audio) / rate
        peak = float(np.abs(audio).max())
        rms = float(np.sqrt(np.mean(audio ** 2)))
        print(f"=== {path.name}  ({duration:.1f} s, peak {20*np.log10(max(peak,1e-6)):.0f} dBFS, "
              f"rms {20*np.log10(max(rms,1e-6)):.0f} dBFS)")

        for label, kwargs in (
            ("plain", {}),
            ("vad", {"vad_filter": True}),
            ("vad+prompt", {"vad_filter": True,
                            "initial_prompt": "Batumi Tower, Adder, Ford, Colt, "
                                              "inbound, final, runway 25, request taxi."}),
            ("vad+hotwords", {"vad_filter": True,
                              "hotwords": stt_hint(
                                  ["Colt", "Ford", "Adder", "Hawg", "Viper",
                                   "Springfield", "Enfield"],
                                  ["Batumi", "Kutaisi", "Gudauta"])}),
        ):
            started = time.monotonic()
            segments, info = model.transcribe(audio, language="en", beam_size=1, **kwargs)
            text = " ".join(s.text.strip() for s in segments).strip()
            ms = (time.monotonic() - started) * 1000
            print(f"  [{label:10s}] ({ms:5.0f} ms) \"{text}\"")
            if kwargs.get("vad_filter"):
                print(f"               vad: speech={info.duration_after_vad:.1f}s "
                      f"of {info.duration:.1f}s")
        print()


if __name__ == "__main__":
    main()
