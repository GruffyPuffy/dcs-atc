"""Minimal headless SRS client: TCP sync + UDP voice receive, Opus -> PCM.

Protocol references:
- ciribob/DCS-SimpleRadioStandalone Common/Network/Client/TCPClientHandler.cs
- ciribob/DCS-SimpleRadioStandalone Common/Models/UDPVoicePacket.cs
- wrycu/srs_recorder srs.py (independent Python implementation)
"""

import json
import socket
import struct
import threading
import time
import uuid

import av
import numpy as np

GUID_LENGTH = 22
PACKET_HEADER_LENGTH = 6  # packet length + audio length + freq part length
FREQUENCY_SEGMENT_LENGTH = 10  # double freq + modulation + encryption
FIXED_PACKET_LENGTH = 4 + 8 + 1 + GUID_LENGTH + GUID_LENGTH

MSG_UPDATE = 0
MSG_PING = 1
MSG_SYNC = 2
MSG_RADIO_UPDATE = 3
MSG_SERVER_SETTINGS = 4
MSG_CLIENT_DISCONNECT = 5
MSG_VERSION_MISMATCH = 6
MSG_EAM_PW = 7
MSG_EAM_DC = 8

MODULATION_DISABLED = 3


def new_guid() -> str:
    # SRS ShortGuid: 22-char ASCII, base64-ish without padding chars
    return uuid.uuid4().hex[:22]


class SrsClient:
    """Connects to an SRS server as a spectator-ish client and yields PCM audio
    per frequency. EAM password lets it transmit/receive without DCS running."""

    def __init__(self, host: str, port: int, name: str, freqs_hz: list[float],
                 eam_password: str | None = None, coalition: int = 0):
        self.host = host
        self.port = port
        self.name = name
        self.freqs_hz = freqs_hz
        self.eam_password = eam_password
        self.coalition = coalition
        self.guid = new_guid()
        self.unit_id = 100000  # arbitrary non-zero, like ExternalAudioClient

        self._tcp: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._decoder = av.codec.CodecContext.create("libopus", "r")
        # libopus decodes to stereo by default; SRS voice is mono. Without this
        # resampler the interleaved stereo bytes saved as mono play at half speed.
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        self._tx_queue: list[bytes] = []
        self._tx_lock = threading.Lock()
        self._tx_thread = None
        self._udp_ready = threading.Event()
        self._running = False
        self._packet_id = 0

        # frequency (Hz, rounded) -> list of float32 PCM chunks (48 kHz mono)
        self.audio: dict[float, list[bytes]] = {f: [] for f in freqs_hz}
        self.clients: dict[str, dict] = {}
        self.on_transmission_start = None  # callable(freq, client_name)
        self.on_transmission_end = None  # callable(freq, pcm_bytes)

        self._stream_start: dict[float, float] = {}
        self._stream_client: dict[float, str] = {}
        self._last_rx: dict[float, float] = {}
        self._watchdog = None
        self._voice_ready_printed = False

    # ---------- TCP ----------

    def _radio_info(self) -> dict:
        radios = [{
            "enc": False, "encKey": 0, "freq": 1.0, "modulation": MODULATION_DISABLED,
            "secFreq": 0.0, "retransmit": False,
        } for _ in range(11)]
        for i, freq in enumerate(self.freqs_hz):
            radios[i + 1].update({"freq": float(freq), "modulation": 0})  # AM
        return {
            "radios": radios,
            "unit": "ATC-Bot",
            "unitId": self.unit_id,
            "iff": {"control": 0, "mode1": 0, "mode3": 0, "mode4": False,
                    "mic": -1, "status": 0},
            "ambient": {"vol": 0.0, "abType": ""},
        }

    def _send(self, msg_type: int, extra: dict | None = None) -> None:
        message = {
            "Client": {
                "ClientGuid": self.guid,
                "Name": self.name,
                "Seat": 0,
                "Coalition": self.coalition,
                "RadioInfo": self._radio_info(),
                "LatLngPosition": {"lat": 0.0, "lng": 0.0, "alt": 0.0},
            },
            "MsgType": msg_type,
            "Version": "2.4.1.0",
        }
        if extra:
            message.update(extra)
        self._tcp.sendall(
            json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def _tcp_loop(self) -> None:
        buffer = b""
        while self._running:
            try:
                data = self._tcp.recv(65536)
            except OSError:
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    self._handle_tcp(line)

    def _handle_tcp(self, line: bytes) -> None:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return
        msg_type = parsed.get("MsgType")
        if msg_type == MSG_SYNC:
            if self.eam_password:
                self._send(MSG_EAM_PW, {"ExternalAWACSModePassword": self.eam_password})
            self._send(MSG_RADIO_UPDATE)
        elif msg_type == MSG_EAM_PW:
            coalition = (parsed.get("Client") or {}).get("Coalition", 0)
            if coalition:
                print(f"[srs] EAM authenticated, coalition={coalition}")
                self.coalition = coalition
            else:
                print("[srs] EAM authentication failed (coalition=0); "
                      "continuing as spectator — receiving may be limited")
        elif msg_type == MSG_EAM_DC:
            print("[srs] EAM authentication FAILED")
        elif msg_type == MSG_VERSION_MISMATCH:
            print("[srs] version mismatch with server")
        elif msg_type in (MSG_UPDATE, MSG_RADIO_UPDATE):
            client = parsed.get("Client") or {}
            if client.get("ClientGuid"):
                self.clients[client["ClientGuid"]] = client
        elif msg_type == MSG_CLIENT_DISCONNECT:
            client = parsed.get("Client") or {}
            self.clients.pop(client.get("ClientGuid"), None)

    # ---------- UDP voice ----------

    def _udp_loop(self) -> None:
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.settimeout(0.5)
        # first ping: our 22-byte GUID, server replies with its own 22 bytes
        self._udp.sendto(self.guid.encode(), (self.host, self.port))
        while self._running:
            try:
                message, _ = self._udp.recvfrom(65535)
            except socket.timeout:
                self._udp.sendto(self.guid.encode(), (self.host, self.port))
                continue
            except OSError:
                break
            if len(message) == GUID_LENGTH:
                if not self._voice_ready_printed:
                    self._voice_ready_printed = True
                    print("[srs] UDP voice link ready")
                self._udp_ready.set()
                continue
            if len(message) > PACKET_HEADER_LENGTH + FIXED_PACKET_LENGTH:
                self._handle_voice(message)

    def _handle_voice(self, message: bytes) -> None:
        try:
            packet = decode_voice_packet(message)
        except Exception:
            return
        if packet is None or not packet["audio_part1_bytes"]:
            return
        try:
            opus_packet = av.Packet(packet["audio_part1_bytes"])
            frames = list(self._decoder.decode(opus_packet))
        except (av.error.InvalidDataError, StopIteration):
            return
        if not frames:
            return
        mono = self._resampler.resample(*frames)
        if not mono:
            return
        pcm = b"".join(bytes(f.planes[0]) for f in mono)
        now = time.monotonic()
        for freq in packet["frequencies"]:
            key = round(freq)
            if key not in self.audio:
                continue
            if key not in self._stream_start:
                self._stream_start[key] = now
                name = self.clients.get(packet["original_client_guid"], {}).get(
                    "Name", packet["original_client_guid"][:8])
                self._stream_client[key] = name
                self._last_rx[key] = now
                if self.on_transmission_start:
                    self.on_transmission_start(key, name)
            self._last_rx[key] = now
            self.audio[key].append(pcm)

    def _watchdog_loop(self) -> None:
        while self._running:
            time.sleep(0.1)
            now = time.monotonic()
            for key in list(self._stream_start):
                if now - self._last_rx.get(key, 0) > 0.4:
                    start = self._stream_start.pop(key)
                    self._last_rx.pop(key, None)
                    name = self._stream_client.pop(key, "?")
                    pcm = b"".join(self.audio[key])
                    self.audio[key] = []
                    duration = now - start
                    if self.on_transmission_end and len(pcm) > 9600:
                        self.on_transmission_end(key, name, pcm, duration)

    # ---------- transmit ----------

    def transmit(self, pcm_48k: bytes, freq_hz: float) -> None:
        """Queue 48 kHz s16 mono PCM for transmission on freq_hz (AM).
        Encodes to 40 ms opus frames (3840 bytes PCM each, like the SRS
        ExternalAudioClient); the TX thread paces them at 40 ms.
        A fresh encoder per transmission: flushing (encode(None)) closes an
        encoder for good, so a reused one would fail on the second reply."""
        encoder = av.codec.CodecContext.create("libopus", "w")
        encoder.format = "s16"
        encoder.layout = "mono"
        encoder.sample_rate = 48000
        encoder.bit_rate = 32000
        try:
            encoder.options = {"frame_duration": "40"}
            encoder.open()
        except Exception:
            pass  # older PyAV: encoder still accepts our 40 ms frames
        frame_bytes = 1920 * 2  # 1920 samples * 2 bytes = 40 ms @ 48 kHz s16 mono
        pad = (-len(pcm_48k)) % frame_bytes
        if pad:
            pcm_48k += b"\x00" * pad
        packets = []
        for i in range(0, len(pcm_48k), frame_bytes):
            chunk = pcm_48k[i:i + frame_bytes]
            frame = av.AudioFrame.from_ndarray(
                np.frombuffer(chunk, dtype=np.int16).reshape(1, -1),
                format="s16", layout="mono")
            frame.sample_rate = 48000
            packets += [bytes(p) for p in encoder.encode(frame)]
        packets += [bytes(p) for p in encoder.encode(None)]
        with self._tx_lock:
            self._tx_queue.append((freq_hz, packets))

    def _tx_loop(self) -> None:
        """Send queued transmissions paced at 40 ms per 40 ms opus frame."""
        packet_id = 1
        while self._running:
            with self._tx_lock:
                item = self._tx_queue.pop(0) if self._tx_queue else None
            if item is None:
                time.sleep(0.05)
                continue
            freq_hz, packets = item
            freq_part = struct.pack("<dBB", float(freq_hz), 0, 0)  # AM, no encryption
            started = time.monotonic()
            for n, opus in enumerate(packets):
                if not self._running:
                    return
                # absolute pacing: packet n goes out at start + n*40 ms
                delay = started + n * 0.040 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                body = struct.pack("<HHH", 0, len(opus), len(freq_part)) + opus + freq_part
                body += struct.pack("<IQ", self.unit_id, packet_id) + b"\x00"
                body += self.guid.encode() + self.guid.encode()
                try:
                    self._udp.sendto(body, (self.host, self.port))
                except OSError:
                    return
                packet_id += 1

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._running = True
        self._tcp = socket.socket()
        self._tcp.connect((self.host, self.port))
        self._send(MSG_SYNC)
        print(f"[srs] TCP connected to {self.host}:{self.port} as {self.name}")
        threading.Thread(target=self._tcp_loop, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        self._tx_thread.start()
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._running = False
        for sock in (self._tcp, self._udp):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass


def decode_voice_packet(message: bytes) -> dict | None:
    """Decode an SRS UDP voice packet (see UDPVoicePacket.cs)."""
    if len(message) < PACKET_HEADER_LENGTH + FIXED_PACKET_LENGTH + FREQUENCY_SEGMENT_LENGTH:
        return None
    receiving_guid = message[-GUID_LENGTH:].decode(errors="replace")
    original_guid = message[-GUID_LENGTH * 2:-GUID_LENGTH].decode(errors="replace")
    body = message[:-GUID_LENGTH * 2]
    retransmit = body[-1]
    packet_length, audio_length, freq_length = struct.unpack_from("<HHH", body, 0)
    freq_count = freq_length // FREQUENCY_SEGMENT_LENGTH
    audio = body[PACKET_HEADER_LENGTH:PACKET_HEADER_LENGTH + audio_length]
    offset = PACKET_HEADER_LENGTH + audio_length
    frequencies, modulations, encryptions = [], [], []
    for _ in range(freq_count):
        freq = struct.unpack_from("<d", body, offset)[0]
        frequencies.append(freq)
        modulations.append(body[offset + 8])
        encryptions.append(body[offset + 9])
        offset += FREQUENCY_SEGMENT_LENGTH
    unit_id = struct.unpack_from("<I", body, offset)[0]
    packet_number = struct.unpack_from("<Q", body, offset + 4)[0]
    return {
        "guid": receiving_guid,
        "original_client_guid": original_guid,
        "audio_part1_bytes": audio,
        "audio_part1_length": audio_length,
        "frequencies": frequencies,
        "modulations": modulations,
        "encryptions": encryptions,
        "unit_id": unit_id,
        "packet_number": packet_number,
        "retransmit": retransmit,
    }
