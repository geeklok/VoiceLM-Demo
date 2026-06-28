from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_asr, routes_health, routes_tts
from app.api.middleware import DrainMiddleware
from app.config import get_settings
from app.engines.registry import EngineRegistry
from app.orchestration.breaker import CircuitBreaker
from app.orchestration.dispatcher import Dispatcher
from app.orchestration.lifecycle import Lifecycle
from app.orchestration.limiter import GpuLimiter
from app.utils.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger = get_logger("app.main")
    settings = get_settings()

    registry = EngineRegistry(settings)
    app.state.registry = registry
    limiter = GpuLimiter(
        asr_concurrency=settings.asr_concurrency,
        tts_concurrency=settings.tts_concurrency,
        acquire_timeout=settings.gpu_acquire_timeout,
        retry_after=settings.gpu_retry_after,
    )
    breaker = CircuitBreaker(
        fail_threshold=settings.breaker_fail_threshold,
        cooldown=settings.breaker_cooldown,
    )
    app.state.dispatcher = Dispatcher(
        registry,
        limiter,
        breaker,
        asr_fallback_enabled=settings.asr_fallback_enabled,
        asr_infer_timeout=settings.asr_infer_timeout,
        node_name=settings.node_name,
        tts_sentence_stream=settings.tts_sentence_stream,
        tts_max_sentence_chars=settings.tts_max_sentence_chars,
        tts_min_sentence_chars=settings.tts_min_sentence_chars,
    )

    # 后台预热, 不阻塞启动; readyz 在预热完成后转为就绪
    async def _warmup() -> None:
        try:
            await registry.warmup()
            logger.info("engines ready")
        except Exception:  # noqa: BLE001
            logger.exception("warmup failed")

    asyncio.create_task(_warmup())
    yield

    # 优雅退出 (Phase 2 §6.5): uvicorn 收到 SIGTERM 触发 lifespan shutdown。
    # 置 draining → /readyz not ready + 中间件拒新请求, 等存量请求清空再退出。
    lifecycle: Lifecycle = app.state.lifecycle
    lifecycle.begin_drain()
    await lifecycle.wait_idle(settings.drain_timeout)
    logger.info("shutdown complete (in_flight=%d)", lifecycle.in_flight)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    lifecycle = Lifecycle()
    app.state.lifecycle = lifecycle
    app.add_middleware(DrainMiddleware, lifecycle=lifecycle)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes_health.router)
    app.include_router(routes_asr.router)
    app.include_router(routes_tts.router)
    return app


app = create_app()
