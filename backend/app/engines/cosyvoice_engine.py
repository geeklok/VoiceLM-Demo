from __future__ import annotations

import asyncio
import os
import sys
from typing import AsyncIterator

import numpy as np

from app.config import Settings
from app.engines.base import TTSEngine
from app.utils.logging import get_logger

logger = get_logger(__name__)


class CosyVoiceEngine(TTSEngine):
    """CosyVoice2 封装 (阿里通义)。原生支持流式合成 (首包 ~150ms)、vLLM、TRT、fp16。

    CosyVoice2-0.5B 原生只暴露 zero-shot 克隆 (inference_zero_shot)。
    内置 SFT 音色需另外下载 CosyVoice-300M-SFT 的 spk2info.pt, 不稳定也不在 MVP 范围。
    因此本服务在加载时把配置里的「参考音频 + 文本」用 add_zero_shot_spk 预注册为命名音色,
    合成时只需传 zero_shot_spk_id, 既稳定又避免每次重复抽取声纹特征。

    CosyVoice 推理为同步生成器, 用线程 + asyncio.Queue 桥接到异步。
    依赖 CosyVoice 仓库 (cosyvoice_repo_dir) 已安装; 本地无 GPU 时改用 stub 引擎。
    """

    name = "cosyvoice2"
    output_sample_rate = 24000

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None
        self._voices: list[str] = []

    @property
    def voices(self) -> list[str]:  # type: ignore[override]
        return self._voices or list(self._settings.cosyvoice_voices.keys())

    def _inject_repo_path(self) -> None:
        repo = self._settings.cosyvoice_repo_dir
        if not repo:
            return
        matcha = os.path.join(repo, "third_party", "Matcha-TTS")
        for p in (repo, matcha):
            if os.path.isdir(p) and p not in sys.path:
                sys.path.insert(0, p)

    def _load(self) -> None:
        if self._model is not None:
            return
        self._inject_repo_path()
        from cosyvoice.cli.cosyvoice import CosyVoice2  # lazy import

        s = self._settings
        logger.info("loading CosyVoice2 model=%s device=%s", s.cosyvoice_model, s.device)
        self._model = CosyVoice2(
            s.cosyvoice_model,
            load_jit=s.cosyvoice_load_jit,
            load_trt=s.cosyvoice_load_trt,
            load_vllm=s.cosyvoice_load_vllm,
            fp16=s.cosyvoice_fp16,
        )
        self.output_sample_rate = self._model.sample_rate

        registered: list[str] = []
        for spk_id, ref in s.cosyvoice_voices.items():
            if not os.path.isfile(ref.audio_path):
                logger.warning("voice %s ref audio missing: %s", spk_id, ref.audio_path)
                continue
            # 该版本 CosyVoice 的 add_zero_shot_spk 内部用 load_wav 重新读取,
            # 入参须为音频文件路径 (传 tensor 会触发 soundfile TypeError)。
            ok = self._model.add_zero_shot_spk(ref.prompt_text, ref.audio_path, spk_id)
            if ok:
                registered.append(spk_id)
            else:
                logger.warning("add_zero_shot_spk failed for voice %s", spk_id)
        self._voices = registered
        logger.info("CosyVoice2 registered voices=%s", registered)

    def is_ready(self) -> bool:
        return self._model is not None

    async def warmup(self) -> None:
        await asyncio.to_thread(self._load)
        if self._voices:
            await self.synthesize("你好", voice=self._voices[0])

    def _resolve_voice(self, voice: str) -> str:
        if voice in self._voices:
            return voice
        default = self._settings.cosyvoice_default_voice
        if default in self._voices:
            return default
        if self._voices:
            return self._voices[0]
        raise RuntimeError("CosyVoice2 无可用音色: 请在配置中预注册参考音频")

    def _generate(self, text: str, voice: str, speed: float, stream: bool):
        return self._model.inference_zero_shot(
            text, "", "", zero_shot_spk_id=voice, stream=stream, speed=speed
        )

    def _collect(self, text: str, voice: str, speed: float) -> list[np.ndarray]:
        self._load()
        spk = self._resolve_voice(voice)
        parts = []
        for out in self._generate(text, spk, speed, stream=False):
            parts.append(out["tts_speech"].numpy().flatten().astype(np.float32))
        return parts

    async def synthesize(self, text: str, voice: str = "default", speed: float = 1.0) -> np.ndarray:
        parts = await asyncio.to_thread(self._collect, text, voice, speed)
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    async def synthesize_stream(
        self, text: str, voice: str = "default", speed: float = 1.0
    ) -> AsyncIterator[np.ndarray]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def _produce() -> None:
            try:
                self._load()
                spk = self._resolve_voice(voice)
                # 流式模式 speed 无效 (CosyVoice 流式不做插值), 这里传 1.0 保持一致
                for out in self._generate(text, spk, 1.0, stream=True):
                    pcm = out["tts_speech"].numpy().flatten().astype(np.float32)
                    loop.call_soon_threadsafe(queue.put_nowait, pcm)
            except Exception as exc:  # noqa: BLE001 - 透传到消费侧
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        asyncio.create_task(asyncio.to_thread(_produce))
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
