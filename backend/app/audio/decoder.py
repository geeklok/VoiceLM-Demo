from __future__ import annotations

import subprocess

import numpy as np

from app.utils.errors import AudioProcessingError


def decode_to_pcm(data: bytes, target_sr: int, target_channels: int = 1) -> np.ndarray:
    """用 ffmpeg 将任意容器/编码的音频解码为 float32 PCM。

    直接让 ffmpeg 完成解码 + 重采样 + downmix, 一步到位输出
    单声道 / target_sr / f32le。返回 float32 ndarray (范围约 [-1, 1])。

    之所以集中用 ffmpeg: 它对 mp3/m4a/webm/opus/wav 等格式兼容最广,
    是工业级 ASR 服务的标准前端 (FunASR runtime 亦内置 ffmpeg)。
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ac", str(target_channels),
        "-ar", str(target_sr),
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True, check=True)
    except FileNotFoundError as exc:  # ffmpeg 未安装
        raise AudioProcessingError("ffmpeg 不可用, 无法解码音频") from exc
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", "ignore")[:500]
        raise AudioProcessingError(f"音频解码失败: {msg}") from exc

    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    if pcm.size == 0:
        raise AudioProcessingError("音频解码结果为空, 可能不是有效音频")
    return pcm.copy()
