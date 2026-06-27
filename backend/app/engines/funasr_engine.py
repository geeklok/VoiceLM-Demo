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
    """FunASR 封装 (阿里 DAMO)。支持两种 flavor:

    - ``sensevoice``: SenseVoiceSmall, 多语种、低延迟、自带富文本后处理 (情感/事件标签)。
    - ``paraformer``: Paraformer-zh, 中文高精度、自带时间戳, 搭配标点模型恢复标点。

    funasr 推理为同步阻塞调用, 统一用 asyncio.to_thread 包裹避免阻塞事件循环。
    依赖 ``pip install -e .[engines]``; 本地无 GPU 时改用 stub 引擎。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        name: str = "funasr-sensevoice",
        flavor: str = "sensevoice",
        model: Optional[str] = None,
        punc_model: str = "",
        languages: Optional[list[str]] = None,
    ) -> None:
        self._settings = settings
        self.name = name
        self._flavor = flavor
        self._model_id = model or settings.funasr_model
        self._punc_model = punc_model
        self.expected_sample_rate = 16000
        self.expected_channels = 1
        self.languages = languages or (
            ["auto", "zh", "en", "yue", "ja", "ko"] if flavor == "sensevoice" else ["zh"]
        )
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from funasr import AutoModel  # lazy import

        s = self._settings
        kwargs: dict = {
            "model": self._model_id,
            "vad_model": s.funasr_vad_model,
            "vad_kwargs": {"max_single_segment_time": s.funasr_vad_max_segment_ms},
            "device": s.device,
            "disable_update": True,
        }
        # Paraformer 自身不含标点, 显式挂标点模型恢复标点 (SenseVoice 自带, 不需要)。
        if self._flavor == "paraformer" and self._punc_model:
            kwargs["punc_model"] = self._punc_model
        logger.info(
            "loading FunASR name=%s model=%s flavor=%s device=%s",
            self.name,
            self._model_id,
            self._flavor,
            s.device,
        )
        self._model = AutoModel(**kwargs)

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

            kwargs = {
                "input": np.ascontiguousarray(pcm, dtype=np.float32),
                "fs": self.expected_sample_rate,
                "cache": {},
                "use_itn": self._settings.funasr_use_itn,
                "batch_size_s": 60,
            }
            # SenseVoice 支持 language 标签; Paraformer-zh 为纯中文模型, 不接受该参数。
            if self._flavor == "sensevoice":
                kwargs["language"] = language if language in _SUPPORTED_LANGS else "auto"
            if hotwords:
                kwargs["hotword"] = " ".join(hotwords)
            res = self._model.generate(**kwargs)

            segments: list[dict] = []
            texts: list[str] = []
            for item in res or []:
                clean = self._postprocess(item.get("text", ""))
                if not clean:
                    continue
                texts.append(clean)
                segments.append({"text": clean, "key": item.get("key", "")})
            return ASRResult(text="".join(texts), segments=segments)

        return await asyncio.to_thread(_run)

    def _postprocess(self, text: str) -> str:
        # SenseVoice 输出含 <|emo|><|event|> 等富文本标记, 需专用后处理剥离。
        if self._flavor == "sensevoice":
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            return rich_transcription_postprocess(text)
        return text.strip()

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
