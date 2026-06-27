from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_asr, routes_health, routes_tts
from app.config import get_settings
from app.engines.registry import EngineRegistry
from app.orchestration.dispatcher import Dispatcher
from app.utils.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger = get_logger("app.main")
    settings = get_settings()

    registry = EngineRegistry(settings)
    app.state.registry = registry
    app.state.dispatcher = Dispatcher(registry)

    # 后台预热, 不阻塞启动; readyz 在预热完成后转为就绪
    async def _warmup() -> None:
        try:
            await registry.warmup()
            logger.info("engines ready")
        except Exception:  # noqa: BLE001
            logger.exception("warmup failed")

    asyncio.create_task(_warmup())
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
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
