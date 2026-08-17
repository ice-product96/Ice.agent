"""G.711 (PCMU/PCMA) 8 kHz <-> PCM16LE 24 kHz helpers for SIP <-> OpenAI Realtime."""

from __future__ import annotations

import struct
from collections import deque
from typing import Any, Literal

try:
    import audioop  # type: ignore
except ModuleNotFoundError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore

Codec = Literal["pcmu", "pcma"]

SIP_RATE = 8000
OPENAI_RATE = 24000
# 20 ms frames are telephony-friendly
FRAME_MS = 20
SIP_FRAME_SAMPLES = SIP_RATE * FRAME_MS // 1000  # 160
OPENAI_FRAME_SAMPLES = OPENAI_RATE * FRAME_MS // 1000  # 480


def pcm16_upsample_8k_to_24k(pcm8: bytes) -> bytes:
    """Linear upsample PCM16LE 8 kHz -> 24 kHz (factor 3)."""
    if not pcm8:
        return b""
    samples = memoryview(pcm8).cast("h")
    out = bytearray(len(samples) * 3 * 2)
    view = memoryview(out).cast("h")
    j = 0
    for i, sample in enumerate(samples):
        nxt = samples[i + 1] if i + 1 < len(samples) else sample
        view[j] = sample
        view[j + 1] = sample + (nxt - sample) // 3
        view[j + 2] = sample + 2 * (nxt - sample) // 3
        j += 3
    return bytes(out)


def pcm16_downsample_24k_to_8k(pcm24: bytes, state: Any = None) -> tuple[bytes, Any]:
    """PCM16LE 24 kHz -> 8 kHz via audioop.ratecv (keep filter state across frames)."""
    if not pcm24:
        return b"", state
    try:
        converted, new_state = audioop.ratecv(pcm24, 2, 1, OPENAI_RATE, SIP_RATE, state)
        return converted, new_state
    except Exception:
        samples = memoryview(pcm24).cast("h")
        out = bytearray((len(samples) // 3) * 2)
        view = memoryview(out).cast("h")
        j = 0
        for i in range(0, len(samples) - 2, 3):
            view[j] = (int(samples[i]) + int(samples[i + 1]) + int(samples[i + 2])) // 3
            j += 1
        return bytes(out), state


def g711_encode(pcm8: bytes, codec: Codec = "pcmu") -> bytes:
    if codec == "pcma":
        return audioop.lin2alaw(pcm8, 2)
    return audioop.lin2ulaw(pcm8, 2)


def g711_decode(payload: bytes, codec: Codec = "pcmu") -> bytes:
    if codec == "pcma":
        return audioop.alaw2lin(payload, 2)
    return audioop.ulaw2lin(payload, 2)


def sip_to_openai(payload: bytes, codec: Codec = "pcmu") -> bytes:
    return pcm16_upsample_8k_to_24k(g711_decode(payload, codec))


def openai_to_sip(pcm24: bytes, codec: Codec = "pcmu", state: Any = None) -> tuple[bytes, Any]:
    pcm8, new_state = pcm16_downsample_24k_to_8k(pcm24, state)
    return g711_encode(pcm8, codec), new_state


class PlaybackBuffer:
    """Thread-safe-ish byte queue for TTS playback toward SIP RTP."""

    def __init__(self, max_bytes: int = 24000 * 2 * 30) -> None:
        self._buf = bytearray()
        self._max = max_bytes
        self.barge_in = False

    def clear(self) -> None:
        self._buf.clear()
        self.barge_in = True

    def append(self, pcm24: bytes) -> None:
        if self.barge_in:
            self.barge_in = False
        self._buf.extend(pcm24)
        if len(self._buf) > self._max:
            del self._buf[: len(self._buf) - self._max]

    def read(self, nbytes: int) -> bytes:
        if nbytes <= 0:
            return b""
        if len(self._buf) >= nbytes:
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            return chunk
        available = bytes(self._buf)
        self._buf.clear()
        return available

    def pending(self) -> int:
        return len(self._buf)


class JitterBuffer:
    def __init__(self, max_packets: int = 50) -> None:
        self._q: deque[bytes] = deque(maxlen=max_packets)

    def push(self, payload: bytes) -> None:
        self._q.append(payload)

    def pop(self) -> bytes | None:
        if not self._q:
            return None
        return self._q.popleft()


def pcm16_rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    return float(audioop.rms(pcm, 2))


def silence_pcm16(samples: int) -> bytes:
    return b"\x00" * (samples * 2)


def pack_pcm16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)
