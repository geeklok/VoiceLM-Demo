from __future__ import annotations

import asyncio
import json

import httpx

from app.config import Settings
from app.engines.agent_client import AgentClient, AgentError


def _settings(**over) -> Settings:
    base = dict(
        agent_endpoint="https://api.example.com/v1",
        agent_api_key="sk-test",
        agent_model="gpt-4o",
        agent_concurrency=2,
        agent_first_token_timeout=5.0,
        agent_timeout=10.0,
        agent_connect_timeout=2.0,
    )
    base.update(over)
    return Settings(**base)


def _sse(*chunks: str) -> bytes:
    lines: list[str] = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
        lines.append("")  # SSE 事件分隔空行
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _client_with_handler(settings: Settings, handler) -> AgentClient:
    client = AgentClient(settings)
    client._client = httpx.AsyncClient(  # 注入 MockTransport, 绕过真实出网
        transport=httpx.MockTransport(handler), timeout=client._timeout
    )
    return client


# ---- _resolve_url --------------------------------------------------------

def test_resolve_url_appends_v1_and_path():
    assert (
        AgentClient._resolve_url("https://api.openai.com")
        == "https://api.openai.com/v1/chat/completions"
    )


def test_resolve_url_keeps_existing_v1():
    assert (
        AgentClient._resolve_url("https://host/v1/")
        == "https://host/v1/chat/completions"
    )


def test_resolve_url_keeps_full_path():
    full = "https://host/v1/chat/completions"
    assert AgentClient._resolve_url(full) == full


def test_resolve_url_empty():
    assert AgentClient._resolve_url("") == ""


def test_configured_requires_url_and_model():
    assert _settings().agent_endpoint
    c = AgentClient(_settings())
    assert c.configured is True
    c2 = AgentClient(_settings(agent_model=""))
    assert c2.configured is False
    c3 = AgentClient(_settings(agent_endpoint=""))
    assert c3.configured is False


# ---- _parse_sse_line -----------------------------------------------------

def test_parse_sse_line_variants():
    p = AgentClient._parse_sse_line
    assert p("") == ""
    assert p(": keep-alive") == ""
    assert p("event: message") == ""  # 非 data 行
    assert p("data: [DONE]") == ""
    assert p("data: not-json") == ""
    assert p('data: {"choices": []}') == ""
    assert p('data: {"choices": [{"delta": {}}]}') == ""
    assert p('data: {"choices": [{"delta": {"content": "你好"}}]}') == "你好"


# ---- stream_chat ---------------------------------------------------------

def test_stream_chat_yields_content_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["model"] == "gpt-4o"
        return httpx.Response(200, content=_sse("你好", "，", "世界。"))

    client = _client_with_handler(_settings(), handler)

    async def run():
        out = []
        async for tok in client.stream_chat([{"role": "user", "content": "hi"}]):
            out.append(tok)
        await client.aclose()
        return out

    assert asyncio.run(run()) == ["你好", "，", "世界。"]


def test_stream_chat_http_error_raises_agent_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error": "bad key"}')

    client = _client_with_handler(_settings(), handler)

    async def run():
        try:
            async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
                pass
            return None
        except AgentError as exc:
            return str(exc)
        finally:
            await client.aclose()

    msg = asyncio.run(run())
    assert msg is not None and "401" in msg


def test_stream_chat_rejects_when_unconfigured():
    client = AgentClient(_settings(agent_model=""))

    async def run():
        try:
            async for _ in client.stream_chat([]):
                pass
            return None
        except AgentError as exc:
            return str(exc)

    assert "未配置" in asyncio.run(run())


def test_stream_chat_rejects_when_concurrency_full():
    client = AgentClient(_settings(agent_concurrency=1))

    async def run():
        client._sem = asyncio.Semaphore(1)
        await client._sem.acquire()  # 占满唯一许可
        try:
            async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
                pass
            return None
        except AgentError as exc:
            return str(exc)

    assert "并发已满" in asyncio.run(run())
