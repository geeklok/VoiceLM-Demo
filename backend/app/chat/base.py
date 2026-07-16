from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import WebSocket


class ChatProvider(ABC):
    """单条语音聊天连接的执行 Provider。"""

    name: str
    mode: str

    @property
    @abstractmethod
    def configured(self) -> bool:
        ...

    @abstractmethod
    async def run(self, ws: WebSocket, start: dict) -> None:
        ...
