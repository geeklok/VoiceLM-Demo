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
    ready = _registry(request).is_ready()
    return HealthResponse(status="ok" if ready else "loading", ready=ready)


@router.get("/api/v1/models", response_model=ModelsResponse)
async def models(request: Request) -> ModelsResponse:
    reg = _registry(request)
    asr, tts = reg.asr, reg.tts
    return ModelsResponse(
        asr=[
            ModelInfo(
                name=asr.name,
                kind="asr",
                expected_sample_rate=asr.expected_sample_rate,
                languages=asr.languages,
            )
        ],
        tts=[
            ModelInfo(
                name=tts.name,
                kind="tts",
                expected_sample_rate=tts.output_sample_rate,
                languages=tts.voices,
            )
        ],
    )
