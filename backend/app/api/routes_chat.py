from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.config import get_settings
from app.engines.agent_client import AgentClient
from app.orchestration.conversation import ConversationOrchestrator
from app.orchestration.dispatcher import Dispatcher
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.websocket("/ws/chat")
async def chat_stream(websocket: WebSocket) -> None:
    """语音对话 (speech-to-speech): 麦克风 → 流式 ASR → 远端 Agent → 流式 TTS。

    未启用 (chat_enabled=False 或缺 agent_endpoint) 时 app.state.agent_client 为 None,
    accept 后立即回 unavailable 并关闭 —— 不影响 ASR/TTS 主链路。
    """
    await websocket.accept()
    agent_client: AgentClient | None = getattr(
        websocket.app.state, "agent_client", None
    )
    if agent_client is None:
        try:
            await websocket.send_json(
                {"type": "error", "code": "unavailable", "message": "语音聊天未启用"}
            )
            await websocket.close()
        except RuntimeError:
            pass
        return

    dispatcher: Dispatcher = websocket.app.state.dispatcher
    settings = get_settings()
    orchestrator = ConversationOrchestrator(dispatcher, agent_client, settings)
    await orchestrator.run(websocket)
