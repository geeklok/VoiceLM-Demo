from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.utils.logging import get_logger

logger = get_logger(__name__)


class ConcurrencyLimitError(Exception):
    """并发许可在 acquire_timeout 内未获取到。路由层据此返回 429 + Retry-After。"""

    def __init__(self, retry_after: int = 2) -> None:
        super().__init__("服务繁忙, 请稍后重试")
        self.retry_after = retry_after


class GpuLimiter:
    """单卡 GPU 并发闸 (Phase 2 §6.3 单机版)。

    ASR / TTS 各持一个信号量, 限制同时占用 GPU 的请求数, 防止显存打爆 / 雪崩。
    超过 acquire_timeout 仍排不到则快速失败 (429), 而非无限堆积。
    """

    def __init__(
        self,
        asr_concurrency: int = 2,
        tts_concurrency: int = 2,
        acquire_timeout: float = 10.0,
        retry_after: int = 2,
    ) -> None:
        self._asr_n = max(1, asr_concurrency)
        self._tts_n = max(1, tts_concurrency)
        self._timeout = acquire_timeout
        self._retry_after = retry_after
        # 信号量延迟到运行中的事件循环内创建 (Py3.9 在构造时绑定 loop)。
        self._asr: asyncio.Semaphore | None = None
        self._tts: asyncio.Semaphore | None = None

    @asynccontextmanager
    async def _slot(self, which: str, kind: str):
        sem = self._ensure(which)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            logger.warning("gpu limiter reject kind=%s (queue timeout)", kind)
            raise ConcurrencyLimitError(retry_after=self._retry_after) from exc
        try:
            yield
        finally:
            sem.release()

    def _ensure(self, which: str) -> asyncio.Semaphore:
        if which == "asr":
            if self._asr is None:
                self._asr = asyncio.Semaphore(self._asr_n)
            return self._asr
        if self._tts is None:
            self._tts = asyncio.Semaphore(self._tts_n)
        return self._tts

    def asr_slot(self):
        return self._slot("asr", "asr")

    def tts_slot(self):
        return self._slot("tts", "tts")
