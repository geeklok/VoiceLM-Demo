from __future__ import annotations

import json

from fastapi import APIRouter, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.chat.cascade import CascadeChatProvider
from app.chat.qwen_realtime import QwenRealtimeProvider
from app.config import Settings, get_settings
from app.engines.agent_client import AgentClient
from app.engines.registry import EngineRegistry
from app.observability.metrics import observe_chat_session
from app.orchestration.dispatcher import Dispatcher
from app.schemas.models import ChatModelInfo, ChatModelsResponse
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get("/api/v1/chat/models", response_model=ChatModelsResponse)
async def chat_models(request: Request) -> ChatModelsResponse:
    settings = get_settings()
    registry: EngineRegistry = request.app.state.registry
    agent: AgentClient | None = getattr(request.app.state, "agent_client", None)
    native: QwenRealtimeProvider | None = getattr(
        request.app.state, "qwen_realtime_provider", None
    )
    return build_chat_model_catalog(settings, registry, agent, native)


def build_chat_model_catalog(
    settings: Settings,
    registry: EngineRegistry,
    agent: AgentClient | None,
    native: QwenRealtimeProvider | None,
) -> ChatModelsResponse:
    if not settings.chat_enabled:
        return ChatModelsResponse()

    models: list[ChatModelInfo] = []
    voices = sorted(
        {
            voice
            for engine in registry.tts_engines.values()
            for voice in engine.voices
        }
    )
    if agent is not None and agent.configured:
        for name in _model_names(
            settings.agent_model, settings.agent_model_allowlist
        ):
            models.append(
                ChatModelInfo(
                    name=name,
                    label=name,
                    mode="cascade",
                    provider="cascade",
                    voices=voices,
                    default=(name == settings.agent_model),
                    input_format="pcm_f32le",
                    supports_thinking=True,
                    supports_barge_in=True,
                    supports_vad_gate=True,
                    preserves_paralinguistics=False,
                )
            )
    if native is not None and native.configured:
        for name in _model_names(
            settings.qwen_omni_model, settings.qwen_omni_model_allowlist
        ):
            models.append(
                ChatModelInfo(
                    name=name,
                    label=name,
                    mode="native",
                    provider=native.name,
                    voices=settings.qwen_omni_voices,
                    default=(name == settings.qwen_omni_model),
                    input_format="pcm_s16le",
                    supports_thinking=False,
                    supports_barge_in=True,
                    supports_vad_gate=False,
                    preserves_paralinguistics=True,
                )
            )
    return ChatModelsResponse(models=models)


def _model_names(default: str, allowlist: list[str]) -> list[str]:
    names: list[str] = []
    for name in [default, *allowlist]:
        if name and name not in names:
            names.append(name)
    return names


@router.websocket("/ws/chat")
async def chat_stream(websocket: WebSocket) -> None:
    """语音对话统一入口：首帧 mode 选择 cascade 或 native Provider。"""
    await websocket.accept()
    try:
        start = await websocket.receive_json()
    except (WebSocketDisconnect, json.JSONDecodeError):
        await websocket.close()
        return
    if not isinstance(start, dict) or start.get("type") != "start":
        await websocket.send_json(
            {"type": "error", "code": "bad_request", "message": "首帧必须为 start"}
        )
        await websocket.close()
        return

    settings = get_settings()
    dispatcher: Dispatcher = websocket.app.state.dispatcher
    agent_client: AgentClient | None = getattr(
        websocket.app.state, "agent_client", None
    )
    mode = str(start.get("mode") or "cascade")

    if mode == "native":
        provider: QwenRealtimeProvider | CascadeChatProvider | None = getattr(
            websocket.app.state, "qwen_realtime_provider", None
        )
        if provider is None:
            model = str(start.get("model") or settings.qwen_omni_model)
            observe_chat_session("qwen-realtime", model, "unavailable")
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unavailable",
                    "message": "原生语音模型未配置",
                }
            )
            await websocket.close()
            return
    elif mode == "cascade":
        provider = CascadeChatProvider(dispatcher, agent_client, settings)
        if not provider.configured:
            model = str(start.get("model") or settings.agent_model or "unknown")
            observe_chat_session("cascade", model, "unavailable")
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unavailable",
                    "message": "级联语音聊天未配置",
                }
            )
            await websocket.close()
            return
    else:
        observe_chat_session(
            "unknown", str(start.get("model") or "unknown"), "bad_request"
        )
        await websocket.send_json(
            {"type": "error", "code": "bad_request", "message": "未知对话模式"}
        )
        await websocket.close()
        return

    await provider.run(websocket, start)
