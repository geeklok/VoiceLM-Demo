from __future__ import annotations

from app.orchestration.lifecycle import Lifecycle

# 排空期间放行的健康检查路径 (LB 需持续探活以摘除本实例)
_HEALTH_PATHS = {"/healthz", "/readyz"}
_BUSY_BODY = b'{"detail":"\\u670d\\u52a1\\u6b63\\u5728\\u5173\\u95ed, \\u8bf7\\u91cd\\u8bd5"}'


class DrainMiddleware:
    """优雅退出中间件 (Phase 2 §6.5)。

    - 跟踪 in-flight 请求数, 供退出时等待存量清空。
    - draining 期间对新请求 (健康检查除外) 快速拒绝: HTTP 503 + Retry-After, WS 直接关闭。
    """

    def __init__(self, app, lifecycle: Lifecycle) -> None:
        self.app = app
        self.lc = lifecycle

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self.lc.draining and path not in _HEALTH_PATHS:
            if scope["type"] == "http":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", b"5"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": _BUSY_BODY})
            else:  # websocket
                await send({"type": "websocket.close", "code": 1013})
            return

        self.lc.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            self.lc.leave()
