from __future__ import annotations

import subprocess
import wave

import numpy as np
import pytest

from app.audio.channel import to_mono
from app.audio.pipeline import preprocess_file, preprocess_pcm
from app.audio.resampler import resample
from app.audio.vad import energy_vad


def _make_stereo_wav_bytes(sample_rate: int, seconds: float = 1.0) -> bytes:
    """生成 48kHz 立体声 16bit WAV: 左声道 440Hz, 右声道 880Hz。"""
    n = int(sample_rate * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    left = 0.5 * np.sin(2 * np.pi * 440 * t)
    right = 0.5 * np.sin(2 * np.pi * 880 * t)
    stereo = np.empty(n * 2, dtype="<i2")
    stereo[0::2] = (left * 32767).astype("<i2")
    stereo[1::2] = (right * 32767).astype("<i2")
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(stereo.tobytes())
    return buf.getvalue()


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg 不可用")
def test_preprocess_48k_stereo_to_16k_mono():
    """验收标准: 48kHz 立体声 → 16kHz 单声道 PCM。"""
    data = _make_stereo_wav_bytes(48000, seconds=1.0)
    result = preprocess_file(data, target_sr=16000, target_channels=1)
    assert result.sample_rate == 16000
    # 1 秒音频, 16kHz 单声道 → 约 16000 采样点
    assert abs(len(result.pcm) - 16000) < 800
    assert result.pcm.dtype == np.float32
    assert 900 <= result.duration_ms <= 1100


def test_to_mono_downmix():
    interleaved = np.array([1.0, 0.0, 0.5, 0.5, -1.0, 1.0], dtype=np.float32)  # 3 帧 x 2 声道
    mono = to_mono(interleaved, channels=2)
    np.testing.assert_allclose(mono, [0.5, 0.5, 0.0], atol=1e-6)


def test_resample_length():
    pcm = np.sin(np.linspace(0, 10, 48000)).astype(np.float32)
    out = resample(pcm, 48000, 16000)
    assert abs(len(out) - 16000) < 50


def test_resample_noop_same_rate():
    pcm = np.zeros(1000, dtype=np.float32)
    out = resample(pcm, 16000, 16000)
    assert len(out) == 1000


def test_preprocess_pcm_negotiation_stereo_resample():
    """流式参数协商: 48kHz 双声道 float32 → 16kHz 单声道。"""
    n = 48000
    interleaved = np.zeros(n * 2, dtype=np.float32)
    interleaved[0::2] = np.sin(np.linspace(0, 100, n)).astype(np.float32)
    out = preprocess_pcm(interleaved, src_sr=48000, src_channels=2, target_sr=16000)
    assert out.dtype == np.float32
    assert abs(len(out) - 16000) < 100


def test_vad_detects_speech_segment():
    sr = 16000
    silence = np.zeros(sr, dtype=np.float32)
    speech = (0.5 * np.sin(np.linspace(0, 500, sr))).astype(np.float32)
    pcm = np.concatenate([silence, speech, silence])
    segments = energy_vad(pcm, sample_rate=sr)
    assert len(segments) >= 1
    s, e = segments[0]
    assert e > s
