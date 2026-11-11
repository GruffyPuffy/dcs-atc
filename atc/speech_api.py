"""Client for an OpenAI-compatible Speaches transcription endpoint."""

from __future__ import annotations

import io
import json
import uuid
import wave
from dataclasses import dataclass
from urllib.request import Request, urlopen

import numpy as np


@dataclass(frozen=True)
class TranscriptionSegment:
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


class SpeachesClient:
    """Send one already-resampled mono float waveform to Speaches."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = 45.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @staticmethod
    def _wav_bytes(audio: np.ndarray) -> bytes:
        pcm = np.clip(audio * 32768.0, -32768, 32767).astype("<i2").tobytes()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        return output.getvalue()

    def transcribe(self, audio: np.ndarray, hotwords: str = "") -> list[TranscriptionSegment]:
        boundary = f"----atc-{uuid.uuid4().hex}"
        chunks = []
        fields = {
            "model": self.model,
            "language": "en",
            "response_format": "verbose_json",
        }
        if hotwords:
            fields["hotwords"] = hotwords
        for name, value in fields.items():
            chunks.extend((
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ))
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="radio.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            self._wav_bytes(audio),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ))
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}/audio/transcriptions",
            data=b"".join(chunks),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read())
        segments = [
            TranscriptionSegment(
                text=str(segment.get("text", "")).strip(),
                no_speech_prob=float(segment.get("no_speech_prob") or 0.0),
                avg_logprob=float(segment.get("avg_logprob") or 0.0),
            )
            for segment in result.get("segments", [])
        ]
        if not segments and result.get("text"):
            segments.append(TranscriptionSegment(text=str(result["text"]).strip()))
        return segments
