from __future__ import annotations

import io
import wave

import numpy as np


def pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int) -> bytes:
    """float32 单声道 PCM → 16bit WAV 字节流。"""
    clipped = np.clip(pcm, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16.tobytes())
    return buf.getvalue()


def pcm_to_int16_bytes(pcm: np.ndarray) -> bytes:
    """float32 → 裸 16bit PCM (流式分包用, 不含 WAV 头)。"""
    clipped = np.clip(pcm, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()
