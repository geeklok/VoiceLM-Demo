from __future__ import annotations

import numpy as np


def resample(pcm: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """重采样 float32 单声道 PCM。优先用 soxr (高质量), 退化到线性插值。"""
    if src_sr == dst_sr:
        return pcm.astype(np.float32, copy=False)
    try:
        import soxr

        return soxr.resample(pcm, src_sr, dst_sr).astype(np.float32)
    except ImportError:
        n_dst = int(round(len(pcm) * dst_sr / src_sr))
        if n_dst <= 0:
            return np.zeros(0, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, num=len(pcm), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
        return np.interp(x_new, x_old, pcm).astype(np.float32)
