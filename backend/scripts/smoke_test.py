#!/usr/bin/env python3
"""真实模型冒烟测试: 不经过 HTTP, 直接调用引擎验证模型可用。

用法:
    python scripts/smoke_test.py --asr     # 合成一段正弦 + 静音, 跑 ASR 转写不报错
    python scripts/smoke_test.py --tts     # 用已注册音色合成 "你好", 写出 wav
    python scripts/smoke_test.py --all

读取 backend/.env 的引擎配置。ASR 用一段测试音频 (优先 --audio 指定的文件)。
TTS 需要先在 .env 配置 COSYVOICE_VOICES 至少一个参考音色。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import wave

import numpy as np

# 允许从 backend/ 直接运行
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.config import get_settings  # noqa: E402
from app.engines.registry import EngineRegistry  # noqa: E402


def _read_wav_f32(path: str, target_sr: int) -> np.ndarray:
    import soundfile as sf

    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != target_sr:
        try:
            import soxr

            mono = soxr.resample(mono, sr, target_sr)
        except Exception:
            ratio = target_sr / sr
            idx = (np.arange(int(len(mono) * ratio)) / ratio).astype(np.int64)
            mono = mono[np.clip(idx, 0, len(mono) - 1)]
    return mono.astype(np.float32)


def _gen_test_pcm(sr: int) -> np.ndarray:
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)
    tone = 0.1 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    return tone


def _write_wav(path: str, pcm: np.ndarray, sr: int) -> None:
    pcm16 = np.clip(pcm, -1, 1)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


async def smoke_asr(reg: EngineRegistry, audio: str | None) -> None:
    asr = reg.asr
    print(f"==> ASR 引擎: {asr.name}, 预热中 (首次会下载/加载权重, 可能较久)...")
    await asr.warmup()
    sr = asr.expected_sample_rate
    pcm = _read_wav_f32(audio, sr) if audio else _gen_test_pcm(sr)
    print(f"    输入 {len(pcm)/sr:.2f}s @ {sr}Hz, 转写中...")
    result = await asr.transcribe(pcm, language="auto")
    print(f"    转写结果: {result.text!r}")
    print(f"    段数: {len(result.segments)}")
    print("==> ASR 冒烟测试通过 ✅")


async def smoke_tts(reg: EngineRegistry, out: str) -> None:
    tts = reg.tts
    print(f"==> TTS 引擎: {tts.name}, 预热中 (首次会下载/加载权重, 可能较久)...")
    await tts.warmup()
    voices = tts.voices
    print(f"    可用音色: {voices}")
    if not voices:
        print("!! 无可用音色: 请在 .env 配置 COSYVOICE_VOICES (参考音频+文本)")
        sys.exit(2)
    voice = voices[0]
    text = "你好，这是 CosyVoice 真实模型的合成测试。"
    print(f"    用音色 {voice!r} 合成: {text!r}")
    pcm = await tts.synthesize(text, voice=voice)
    _write_wav(out, pcm, tts.output_sample_rate)
    print(f"    输出 {len(pcm)/tts.output_sample_rate:.2f}s @ {tts.output_sample_rate}Hz -> {out}")
    print("==> TTS 冒烟测试通过 ✅")


async def amain() -> int:
    ap = argparse.ArgumentParser(description="真实模型冒烟测试")
    ap.add_argument("--asr", action="store_true")
    ap.add_argument("--tts", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--audio", help="ASR 测试用音频文件 (任意采样率/声道)")
    ap.add_argument("--out", default="smoke_tts_out.wav", help="TTS 输出 wav 路径")
    args = ap.parse_args()

    if not (args.asr or args.tts or args.all):
        ap.print_help()
        return 1

    settings = get_settings()
    print(f"==> 配置: asr_engine={settings.asr_engine} tts_engine={settings.tts_engine} device={settings.device}")
    reg = EngineRegistry(settings)

    if args.all or args.asr:
        await smoke_asr(reg, args.audio)
    if args.all or args.tts:
        await smoke_tts(reg, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
