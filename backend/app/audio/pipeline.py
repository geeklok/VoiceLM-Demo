from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.audio.channel import peak_normalize, to_mono
from app.audio.decoder import decode_to_pcm
from app.audio.resampler import resample
from app.utils.errors import UnsupportedParameterError
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PreprocessResult:
    pcm: np.ndarray  # float32 单声道, 采样率 = target_sr
    sample_rate: int
    duration_ms: int


def preprocess_file(
    data: bytes, target_sr: int, target_channels: int = 1, normalize: bool = True
) -> PreprocessResult:
    """文件式预处理: 任意格式 bytes → 模型契约 PCM。

    ffmpeg 已完成解码 + 重采样 + downmix, 一步到位。
    """
    pcm = decode_to_pcm(data, target_sr=target_sr, target_channels=target_channels)
    if normalize:
        pcm = peak_normalize(pcm)
    duration_ms = int(len(pcm) / target_sr * 1000)
    return PreprocessResult(pcm=pcm, sample_rate=target_sr, duration_ms=duration_ms)


def preprocess_pcm(
    pcm: np.ndarray, src_sr: int, src_channels: int, target_sr: int, normalize: bool = False
) -> np.ndarray:
    """流式预处理: 已解码 PCM 的参数协商 (重采样 + 声道归一)。

    用于 WebSocket 流式场景 — 客户端发来原始 PCM, 这里把它适配到模型契约。
    若参数无法适配则抛出 UnsupportedParameterError (映射 400)。
    """
    if pcm.dtype != np.float32:
        raise UnsupportedParameterError(f"PCM 必须为 float32, 收到 {pcm.dtype}")
    if src_channels < 1:
        raise UnsupportedParameterError(f"非法声道数: {src_channels}")

    out = to_mono(pcm, src_channels)
    out = resample(out, src_sr, target_sr)
    if normalize:
        out = peak_normalize(out)
    return out
