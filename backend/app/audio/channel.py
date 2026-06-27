from __future__ import annotations

import numpy as np


def to_mono(pcm: np.ndarray, channels: int) -> np.ndarray:
    """将交错的多声道 PCM downmix 为单声道 (取通道均值)。"""
    if channels <= 1:
        return pcm.astype(np.float32, copy=False)
    usable = (len(pcm) // channels) * channels
    frames = pcm[:usable].reshape(-1, channels)
    return frames.mean(axis=1).astype(np.float32)


def peak_normalize(pcm: np.ndarray, target_peak: float = 0.95) -> np.ndarray:
    """峰值归一化, 避免过小/过大音量影响模型表现。"""
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak < 1e-6:
        return pcm
    return (pcm * (target_peak / peak)).astype(np.float32)
