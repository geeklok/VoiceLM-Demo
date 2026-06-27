from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Optional

import numpy as np

from app.audio.pipeline import preprocess_file, preprocess_pcm
from app.engines.base import ASRPartial
from app.engines.registry import EngineRegistry
from app.observability.metrics import (
    INFERENCE_FAILURES,
    observe_asr,
    observe_tts,
)
from app.orchestration.breaker import CircuitBreaker
from app.orchestration.limiter import ConcurrencyLimitError, GpuLimiter
from app.schemas.models import ASRResponse, ASRSegment
from app.utils.logging import get_logger

logger = get_logger(__name__)


class Dispatcher:
    """L2 编排层: 串联预处理 (L3) → 引擎 (L4) → 后处理 (L5)。

    所有引擎调用经 GpuLimiter 限流 (Phase 2 §6.3): 占用 GPU 期间持有信号量许可,
    超并发的请求排队, 排队超时快速失败 (429)。

    文件式 ASR 走降级链 + 熔断 (Phase 2 §6.4): 主引擎超时/OOM/异常时自动尝试下一个引擎,
    连续失败的引擎被熔断短路跳过; 响应标注实际使用模型与是否降级。
    """

    def __init__(
        self,
        registry: EngineRegistry,
        limiter: Optional[GpuLimiter] = None,
        breaker: Optional[CircuitBreaker] = None,
        *,
        asr_fallback_enabled: bool = True,
        asr_infer_timeout: float = 0.0,
    ) -> None:
        self._registry = registry
        self._limiter = limiter or GpuLimiter()
        self._breaker = breaker or CircuitBreaker()
        self._fallback_enabled = asr_fallback_enabled
        self._infer_timeout = asr_infer_timeout

    async def asr_file(
        self,
        data: bytes,
        language: str = "auto",
        hotwords: Optional[list[str]] = None,
        model: Optional[str] = None,
    ) -> ASRResponse:
        chain = self._registry.asr_chain(model)
        if not self._fallback_enabled:
            chain = chain[:1]
        primary_name = chain[0].name

        last_exc: Optional[Exception] = None
        for engine in chain:
            if self._breaker.is_open(engine.name):
                logger.warning("skip engine=%s (circuit open)", engine.name)
                continue
            pre = preprocess_file(
                data,
                target_sr=engine.expected_sample_rate,
                target_channels=engine.expected_channels,
            )
            try:
                async with self._limiter.asr_slot():
                    t0 = time.perf_counter()
                    coro = engine.transcribe(pre.pcm, language=language, hotwords=hotwords)
                    if self._infer_timeout > 0:
                        result = await asyncio.wait_for(coro, timeout=self._infer_timeout)
                    else:
                        result = await coro
                    process_ms = int((time.perf_counter() - t0) * 1000)
            except ConcurrencyLimitError:
                # 限流是过载信号, 不是引擎故障; 不计熔断, 直接上抛 (路由层转 429)。
                raise
            except Exception as exc:  # noqa: BLE001
                self._breaker.record_failure(engine.name)
                INFERENCE_FAILURES.labels(engine=engine.name).inc()
                last_exc = exc
                logger.warning(
                    "asr engine=%s failed (%s); trying fallback", engine.name, exc
                )
                continue

            self._breaker.record_success(engine.name)
            rtf = (process_ms / pre.duration_ms) if pre.duration_ms else 0.0
            observe_asr(
                engine.name,
                status="ok",
                degraded=(engine.name != primary_name),
                process_ms=process_ms,
                audio_ms=pre.duration_ms,
                rtf=rtf,
            )
            return ASRResponse(
                text=result.text,
                segments=[ASRSegment(**s) for s in result.segments],
                audio_duration_ms=pre.duration_ms,
                process_ms=process_ms,
                rtf=round(rtf, 4),
                model=engine.name,
                degraded=(engine.name != primary_name),
            )

        # 所有引擎均失败或被熔断
        observe_asr(
            primary_name, status="error", degraded=False,
            process_ms=0, audio_ms=0, rtf=0.0,
        )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ASR 服务暂不可用 (所有引擎已熔断)")

    async def asr_stream(
        self,
        pcm_chunks: AsyncIterator[tuple[np.ndarray, int, int]],
        language: str = "auto",
        model: Optional[str] = None,
    ) -> AsyncIterator[ASRPartial]:
        engine = self._registry.asr(model)
        target_sr = engine.expected_sample_rate

        async def adapted() -> AsyncIterator[np.ndarray]:
            async for raw, src_sr, src_ch in pcm_chunks:
                yield preprocess_pcm(raw, src_sr, src_ch, target_sr)

        async with self._limiter.asr_slot():
            async for partial in engine.transcribe_stream(adapted(), language=language):
                yield partial

    async def tts_file(
        self, text: str, voice: str, speed: float, model: Optional[str] = None
    ) -> tuple[np.ndarray, int]:
        engine = self._registry.tts(model)
        sr = engine.output_sample_rate
        try:
            async with self._limiter.tts_slot():
                t0 = time.perf_counter()
                pcm = await engine.synthesize(text, voice=voice, speed=speed)
                process_ms = int((time.perf_counter() - t0) * 1000)
        except ConcurrencyLimitError:
            raise
        except Exception:
            observe_tts(engine.name, "file", status="error")
            raise
        audio_ms = int(len(pcm) / sr * 1000) if sr else 0
        rtf = (process_ms / audio_ms) if audio_ms else None
        observe_tts(
            engine.name, "file", status="ok",
            process_ms=process_ms, rtf=rtf,
        )
        return pcm, sr

    async def tts_stream(
        self, text: str, voice: str, speed: float, model: Optional[str] = None
    ) -> tuple[AsyncIterator[np.ndarray], int]:
        engine = self._registry.tts(model)
        sr = engine.output_sample_rate

        async def guarded() -> AsyncIterator[np.ndarray]:
            try:
                async with self._limiter.tts_slot():
                    t0 = time.perf_counter()
                    first = True
                    n_samples = 0
                    async for chunk in engine.synthesize_stream(
                        text, voice=voice, speed=speed
                    ):
                        if first:
                            ttfb_ms = int((time.perf_counter() - t0) * 1000)
                            first = False
                        n_samples += len(chunk)
                        yield chunk
                    process_ms = int((time.perf_counter() - t0) * 1000)
                audio_ms = int(n_samples / sr * 1000) if sr else 0
                observe_tts(
                    engine.name, "stream", status="ok",
                    process_ms=process_ms,
                    ttfb_ms=ttfb_ms if not first else None,
                    rtf=(process_ms / audio_ms) if audio_ms else None,
                )
            except ConcurrencyLimitError:
                raise
            except Exception:
                observe_tts(engine.name, "stream", status="error")
                raise

        return guarded(), sr