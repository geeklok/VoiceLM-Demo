from __future__ import annotations

import numpy as np


def energy_vad(
    pcm: np.ndarray,
    sample_rate: int,
    frame_ms: int = 30,
    threshold_db: float = -40.0,
    min_speech_ms: int = 200,
) -> list[tuple[int, int]]:
    """轻量能量 VAD, 返回语音段 [(start_sample, end_sample)]。

    MVP 占位实现, 无外部依赖。生产环境由 FunASR FSMN-VAD 替代
    (见 funasr_engine 的 vad_model)。此实现用于流式分段与单测。
    """
    if pcm.size == 0:
        return []
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    n_frames = len(pcm) // frame_len
    if n_frames == 0:
        return [(0, len(pcm))]

    frames = pcm[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    voiced = db > threshold_db

    segments: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        elif not v and start is not None:
            segments.append((start * frame_len, i * frame_len))
            start = None
    if start is not None:
        segments.append((start * frame_len, len(pcm)))

    min_len = int(sample_rate * min_speech_ms / 1000)
    return [(s, e) for s, e in segments if e - s >= min_len]
