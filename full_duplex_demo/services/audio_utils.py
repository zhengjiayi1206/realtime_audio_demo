import struct
import sys
import wave
from array import array
from io import BytesIO


def float32_bytes_to_list(data: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(data[: len(data) - (len(data) % 4)])
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tolist()


def resample_linear(samples: list[float], src_rate: int, tgt_rate: int) -> list[float]:
    if not samples or src_rate == tgt_rate:
        return samples
    n_out = max(1, int(len(samples) * tgt_rate / src_rate))
    ratio = src_rate / tgt_rate
    last = len(samples) - 1
    out: list[float] = []
    for i in range(n_out):
        pos = i * ratio
        left = int(pos)
        right = min(left + 1, last)
        frac = pos - left
        out.append(samples[left] * (1.0 - frac) + samples[right] * frac)
    return out


def pcm_float_to_wav_bytes(samples: list[float], sample_rate: int) -> bytes:
    frames = bytearray()
    for s in samples:
        clipped = max(-1.0, min(1.0, s))
        frames.extend(struct.pack("<h", int(clipped * 32767.0)))
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def wav_bytes_from_float32_chunks(
    chunks: list[bytes], src_rate: int, tgt_rate: int
) -> bytes:
    samples: list[float] = []
    for c in chunks:
        samples.extend(float32_bytes_to_list(c))
    if src_rate != tgt_rate:
        samples = resample_linear(samples, src_rate, tgt_rate)
    return pcm_float_to_wav_bytes(samples, tgt_rate)
