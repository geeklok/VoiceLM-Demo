from __future__ import annotations

import time

from app.utils.logging import get_logger

logger = get_logger(__name__)


class CircuitBreaker:
    """轻量熔断器 (Phase 2 §6.4)。按 key (引擎名) 跟踪连续失败次数。

    连续失败达到 fail_threshold 即「打开」, cooldown 秒内对该 key 短路 (allow 返回 False),
    让降级链跳过故障引擎、避免持续把请求往坏引擎上撞。冷却期满后半开放行一次试探,
    成功则复位, 失败则重新打开。
    """

    def __init__(self, fail_threshold: int = 3, cooldown: float = 30.0) -> None:
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown = cooldown
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        until = self._open_until.get(key, 0.0)
        if until and time.monotonic() < until:
            return False
        return True

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._open_until.pop(key, None)

    def record_failure(self, key: str) -> None:
        n = self._fails.get(key, 0) + 1
        self._fails[key] = n
        if n >= self._fail_threshold:
            self._open_until[key] = time.monotonic() + self._cooldown
            logger.warning(
                "circuit open key=%s fails=%d cooldown=%.0fs", key, n, self._cooldown
            )

    def is_open(self, key: str) -> bool:
        return not self.allow(key)
