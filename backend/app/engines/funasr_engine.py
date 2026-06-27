from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

import numpy as np

from app.config import Settings
from app.engines.base import ASREngine, ASRPartial, ASRResult
from app.utils.logging import get_logger

logger = get_logger(__name__)

# SenseVoiceSmall 支持的语言标签; 其余值回退到 auto
_SUPPORTED_LANGS = {"auto", "zh", "en", "yue", "ja", "ko", "nospeech"}


class FunASREngine(ASREngine):
    """FunASR 封装 (阿里 DAMO)。MVP 使用 SenseVoiceSmall + FSMN-VAD。

    funasr 推理为同步阻塞调用, 统一用 asyncio.to_thread 包裹避免阻塞事件循环。
    依赖 ``pip install -e .[engines]``; 本地无 GPU 时改用 stub 引擎。
    """

    name = "funasr-sensevoice"
    expected_sample_rate = 16000
    expected_channels = 1
    languages = ["auto", "zh", "en", "yue", "ja", "ko"]

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from funasr import AutoModel  # lazy import

        s = self._settings
        logger.info(
            "loading FunASR model=%s vad=%s device=%s", s.funasr_model, s.funasr_vad_model, s.device
        )
        self._model = AutoModel(
            model=s.funasr_model,
            vad_model=s.funasr_vad_model,
            vad_kwargs={"max_single_segment_time": s.funasr_vad_max_segment_ms},
            device=s.device,
            disable_update=True,
        )

    def is_ready(self) -> bool:
        return self._model is not None

    async def warmup(self) -> None:
        await asyncio.to_thread(self._load)
        silence = np.zeros(self.expected_sample_rate, dtype=np.float32)
        await self.transcribe(silence)

    async def transcribe(
        self, pcm: np.ndarray, language: str = "auto", hotwords: Optional[list[str]] = None
    ) -> ASRResult:
        def _run() -> ASRResult:
            self._load()
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            lang = language if language in _SUPPORTED_LANGS else "auto"
            kwargs = {
                "input": np.ascontiguousarray(pcm, dtype=np.float32),
                "fs": self.expected_sample_rate,
                "cache": {},
                "language": lang,
                "use_itn": self._settings.funasr_use_itn,
                "batch_size_s": 60,
            }
            if hotwords:
                kwargs["hotword"] = " ".join(hotwords)
            res = self._model.generate(**kwargs)

            segments: list[dict] = []
            texts: list[str] = []
            for item in res or []:
                clean = rich_transcription_postprocess(item.get("text", ""))
                if not clean:
                    continue
                texts.append(clean)
                segments.append({"text": clean, "key": item.get("key", "")})
            return ASRResult(text="".join(texts), segments=segments)

        return await asyncio.to_thread(_run)

    async def transcribe_stream(
        self, chunks: AsyncIterator[np.ndarray], language: str = "auto"
    ) -> AsyncIterator[ASRPartial]:
        # MVP: 累积音频后整段转写 (SenseVoice 为非自回归, 走 offline pass)。
        # 真正的低延迟流式 partial 在 Phase 3 引入 paraformer-streaming / 2pass。
        buf: list[np.ndarray] = []
        async for chunk in chunks:
            buf.append(chunk)
            secs = sum(len(c) for c in buf) / self.expected_sample_rate
            yield ASRPartial(text=f"... ({secs:.1f}s)", is_final=False)
        pcm = np.concatenate(buf) if buf else np.zeros(0, dtype=np.float32)
        result = await self.transcribe(pcm, language=language)
        yield ASRPartial(text=result.text, is_final=True)
