from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.orchestration.lifecycle import Lifecycle


def test_lifecycle_counts_in_flight():
    async def run():
        lc = Lifecycle()
        assert lc.in_flight == 0
        lc.enter()
        lc.enter()
        assert lc.in_flight == 2
        lc.leave()
        assert lc.in_flight == 1
        # 还有 1 个在途, wait_idle 应超时返回 False
        ok = await lc.wait_idle(timeout=0.05)
        assert ok is False
        lc.leave()
        ok = await lc.wait_idle(timeout=0.05)
        assert ok is True

    asyncio.run(run())


def test_drain_blocks_when_in_flight_then_completes():
    async def run():
        lc = Lifecycle()
        lc.enter()
        lc.begin_drain()
        assert lc.draining is True

        async def finish_later():
            await asyncio.sleep(0.05)
            lc.leave()

        t = asyncio.create_task(finish_later())
        ok = await lc.wait_idle(timeout=1.0)
        assert ok is True
        await t

    asyncio.run(run())


def test_readyz_reports_draining():
    app = create_app()
    with TestClient(app) as c:
        # 正常: ready (stub 引擎瞬时就绪)
        r = c.get("/readyz")
        assert r.status_code == 200
        # 进入排空
        app.state.lifecycle.begin_drain()
        r = c.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is False
        assert body["status"] == "draining"


def test_new_requests_rejected_while_draining():
    app = create_app()
    with TestClient(app) as c:
        app.state.lifecycle.begin_drain()
        # 健康检查仍放行
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 200
        # 业务请求被拒
        r = c.get("/api/v1/models")
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "5"
