from __future__ import annotations

from fastapi import APIRouter, Request

from app.engines.registry import EngineRegistry
from app.schemas.models import HealthResponse, ModelInfo, ModelsResponse

router = APIRouter()


def _registry(request: Request) -> EngineRegistry:
    return request.app.state.registry


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", ready=_registry(request).is_ready())


@router.get("/readyz", response_model=HealthResponse)
async def readyz(request: Request) -> HealthResponse:
    # 排空中: 报 not ready, 让 LB/健康检查把本实例摘除 (Phase 2 §6.5)
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is not None and lifecycle.draining:
        return HealthResponse(status="draining", ready=False)
    ready = _registry(request).is_ready()
    return HealthResponse(status="ok" if ready else "loading", ready=ready)


@router.get("/api/v1/models", response_model=ModelsResponse)
async def models(request: Request) -> ModelsResponse:
    reg = _registry(request)
    return ModelsResponse(
        asr=[
            ModelInfo(
                name=e.name,
                kind="asr",
                expected_sample_rate=e.expected_sample_rate,
                languages=e.languages,
                default=(e.name == reg.default_asr),
            )
            for e in reg.asr_engines.values()
        ],
        tts=[
            ModelInfo(
                name=e.name,
                kind="tts",
                expected_sample_rate=e.output_sample_rate,
                languages=e.voices,
                default=(e.name == reg.default_tts),
            )
            for e in reg.tts_engines.values()
        ],
    )
