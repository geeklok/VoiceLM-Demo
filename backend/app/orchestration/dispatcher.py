from __future__ import annotations

import time
from typing import AsyncIterator, Optional

import numpy as np

from app.audio.pipeline import preprocess_file, preprocess_pcm
from app.engines.base import ASRPartial
from app.engines.registry import EngineRegistry
from app.schemas.models import ASRResponse, ASRSegment


class Dispatcher:
    """L2 编排层: 串联预处理 (L3) → 引擎 (L4) → 后处理 (L5)。"""

    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry

    async def asr_file(
        self, data: bytes, language: str = "auto", hotwords: Optional[list[str]] = None
    ) -> ASRResponse:
        engine = self._registry.asr
        pre = preprocess_file(
            data, target_sr=engine.expected_sample_rate, target_channels=engine.expected_channels
        )
        t0 = time.perf_counter()
        result = await engine.transcribe(pre.pcm, language=language, hotwords=hotwords)
        process_ms = int((time.perf_counter() - t0) * 1000)
        rtf = (process_ms / pre.duration_ms) if pre.duration_ms else 0.0
        return ASRResponse(
            text=result.text,
            segments=[ASRSegment(**s) for s in result.segments],
            audio_duration_ms=pre.duration_ms,
            process_ms=process_ms,
            rtf=round(rtf, 4),
            model=engine.name,
        )

    async def asr_stream(
        self, pcm_chunks: AsyncIterator[tuple[np.ndarray, int, int]], language: str = "auto"
    ) -> AsyncIterator[ASRPartial]:
        engine = self._registry.asr
        target_sr = engine.expected_sample_rate

        async def adapted() -> AsyncIterator[np.ndarray]:
            async for raw, src_sr, src_ch in pcm_chunks:
                yield preprocess_pcm(raw, src_sr, src_ch, target_sr)

        async for partial in engine.transcribe_stream(adapted(), language=language):
            yield partial

    async def tts_file(self, text: str, voice: str, speed: float) -> tuple[np.ndarray, int]:
        engine = self._registry.tts
        pcm = await engine.synthesize(text, voice=voice, speed=speed)
        return pcm, engine.output_sample_rate

    async def tts_stream(
        self, text: str, voice: str, speed: float
    ) -> tuple[AsyncIterator[np.ndarray], int]:
        engine = self._registry.tts
        return engine.synthesize_stream(text, voice=voice, speed=speed), engine.output_sample_rate
