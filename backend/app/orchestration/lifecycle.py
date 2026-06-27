from __future__ import annotations

import asyncio

from app.utils.logging import get_logger

logger = get_logger(__name__)


class Lifecycle:
    """进程生命周期状态 (Phase 2 §6.5 优雅退出)。

    draining=True 后: /readyz 转 not ready (供 LB/健康检查摘除), 中间件拒绝新请求 (503),
    存量 in-flight 请求处理完毕后进程才退出。
    """

    def __init__(self) -> None:
        self.draining = False
        self._in_flight = 0
        self._idle: asyncio.Event | None = None

    def _idle_event(self) -> asyncio.Event:
        # asyncio.Event 在 Py3.9 构造时绑定 loop, 延迟到运行中的循环内创建。
        if self._idle is None:
            self._idle = asyncio.Event()
            if self._in_flight == 0:
                self._idle.set()
        return self._idle

    def begin_drain(self) -> None:
        if not self.draining:
            self.draining = True
            logger.info("lifecycle: draining started (in_flight=%d)", self._in_flight)

    def enter(self) -> None:
        self._in_flight += 1
        if self._idle is not None:
            self._idle.clear()

    def leave(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0 and self._idle is not None:
            self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def wait_idle(self, timeout: float) -> bool:
        """等待存量请求清空。返回 True=已清空, False=超时仍有残留。"""
        if self._in_flight == 0:
            return True
        idle = self._idle_event()
        try:
            await asyncio.wait_for(idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "lifecycle: drain timeout, %d requests still in flight", self._in_flight
            )
            return False
