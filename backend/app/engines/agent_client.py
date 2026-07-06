from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

import httpx

from app.config import Settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


class AgentError(Exception):
    """远端 Agent 调用失败 (网络/超时/非 2xx/协议异常/过载)。路由层据此下发 error 帧。"""


class AgentClient:
    """L4: 远端 AI Agent 客户端 (OpenAI 兼容 /v1/chat/completions, stream=true)。

    只消费文本流: 逐条解析 SSE, yield delta.content 增量。不占 GPU,
    用独立的 httpx.AsyncClient (连接池复用)。兼容 OpenAI / vLLM / DashScope(兼容模式) /
    火山方舟 等; 换服务只改 agent_endpoint / agent_model / agent_api_key。

    endpoint 归一: 允许配 "https://host" 或 "https://host/v1", 统一拼成
    ".../v1/chat/completions" (已带 /v1 则不重复加)。
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._url = self._resolve_url(settings.agent_endpoint)
        # 超时拆分: 连接 / 读 (首/后续 token 间隔) / 写 / 池等待。
        # read 取 first_token_timeout 与 timeout 的较大者, 兜底长回复。
        read_to = max(settings.agent_first_token_timeout, settings.agent_timeout)
        self._timeout = httpx.Timeout(
            connect=settings.agent_connect_timeout,
            read=read_to,
            write=settings.agent_connect_timeout,
            pool=settings.agent_connect_timeout,
        )
        self._client: Optional[httpx.AsyncClient] = None
        # 出网并发闸: 延迟到运行中的事件循环内创建 (Py3.9 构造时会绑 loop)。
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_n = max(1, settings.agent_concurrency)

    @staticmethod
    def _resolve_url(endpoint: str) -> str:
        base = (endpoint or "").rstrip("/")
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + "/chat/completions"

    @property
    def configured(self) -> bool:
        return bool(self._url and self._s.agent_model)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._s.agent_api_key:
            h["Authorization"] = f"Bearer {self._s.agent_api_key}"
        return h

    def _payload(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
    ) -> dict:
        return {
            "model": model or self._s.agent_model,
            "messages": messages,
            "stream": True,
            "temperature": self._s.agent_temperature,
            "max_tokens": self._s.agent_max_tokens,
            # 混合推理模型 (Qwen3 系列) 的思考开关; 语音场景默认关以降首 token 延迟。
            # 顶层字段 (非 extra_body): DashScope/百炼兼容模式实测仅顶层生效; 不支持
            # 该参数的模型 (如 qwen-plus) 会兼容忽略。None 时回退全局默认。
            "enable_thinking": (
                self._s.agent_enable_thinking
                if enable_thinking is None
                else bool(enable_thinking)
            ),
        }

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
    ) -> AsyncIterator[str]:
        """流式对话: POST 远端, 逐行解析 SSE, yield 非空 content 增量。

        model / enable_thinking 为逐连接覆盖 (None=用全局默认); 单例 client 无状态,
        并发连接各传各的, 互不串扰。

        SSE 约定: 每条事件以 'data: ' 开头; 'data: [DONE]' 表示结束;
        其余为 JSON, 取 choices[0].delta.content (缺字段安全跳过)。
        非 2xx / 网络异常 / 超时 -> 抛 AgentError。
        """
        if not self.configured:
            raise AgentError("Agent 未配置 (缺 endpoint 或 model)")

        if self._sem is None:
            self._sem = asyncio.Semaphore(self._sem_n)
        if self._sem.locked():
            # 许可已耗尽: 不排队, 直接拒 (聊天是实时交互, 排队等待没意义)。
            raise AgentError("Agent 并发已满, 请稍后再试")

        client = self._ensure_client()
        await self._sem.acquire()
        try:
            async with client.stream(
                "POST", self._url, headers=self._headers(),
                json=self._payload(messages, model=model, enable_thinking=enable_thinking),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                    raise AgentError(f"Agent HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    content = self._parse_sse_line(line)
                    if content:
                        yield content
        except AgentError:
            raise
        except httpx.TimeoutException as exc:
            raise AgentError(f"Agent 超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AgentError(f"Agent 网络错误: {exc}") from exc
        finally:
            self._sem.release()

    @staticmethod
    def _parse_sse_line(line: str) -> str:
        """解析一行 SSE, 返回该行的 content 增量 (无则空串)。

        返回空串的情况: 空行/心跳、注释 (':' 开头)、[DONE]、非 data 行、缺 content。
        """
        line = line.strip()
        if not line or line.startswith(":"):
            return ""
        if not line.startswith("data:"):
            return ""
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            return ""
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return ""
        choices = obj.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        return delta.get("content") or ""
