from __future__ import annotations

from fastapi import WebSocket

from app.chat.base import ChatProvider
from app.config import Settings
from app.engines.agent_client import AgentClient
from app.orchestration.conversation import ConversationOrchestrator
from app.orchestration.dispatcher import Dispatcher


class CascadeChatProvider(ChatProvider):
    """现有 ASR -> 文本 Agent -> TTS 级联链路。"""

    name = "cascade"
    mode = "cascade"

    def __init__(
        self,
        dispatcher: Dispatcher,
        agent_client: AgentClient | None,
        settings: Settings,
    ) -> None:
        self._dispatcher = dispatcher
        self._agent = agent_client
        self._settings = settings

    @property
    def configured(self) -> bool:
        return self._agent is not None and self._agent.configured

    async def run(self, ws: WebSocket, start: dict) -> None:
        if self._agent is None:
            raise RuntimeError("级联语音聊天未配置")
        orchestrator = ConversationOrchestrator(
            self._dispatcher, self._agent, self._settings
        )
        await orchestrator.run(ws, start=start)
