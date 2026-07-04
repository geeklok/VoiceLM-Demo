from __future__ import annotations

import asyncio
import json
from collections import deque

import numpy as np

from app.config import Settings
from app.engines.base import ASRPartial
from app.orchestration.conversation import ConversationOrchestrator


class _FakeAgent:
    """假远端 Agent: configured=True, 按预设 token 序列流式产出。"""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.seen_messages: list[dict] | None = None

    @property
    def configured(self) -> bool:
        return True

    async def stream_chat(self, messages):
        self.seen_messages = list(messages)
        for tok in self._tokens:
            yield tok


class _FakeDispatcher:
    """假编排层: ASR 读一个 PCM 块即出 partial+final; TTS 产出固定 PCM 块。"""

    def __init__(self, final_text: str) -> None:
        self._final = final_text
        self.tts_calls: list[str] = []

    @property
    def node(self) -> str:
        return "node-test"

    async def asr_stream(self, chunks, language="auto", model=None):
        got = False
        async for _chunk in chunks:
            got = True
            yield ASRPartial(text="你", is_final=False, segment_id=0)
            break  # 拿到一块即认定句末, 不阻塞在空队列
        if got:
            yield ASRPartial(text=self._final, is_final=True, segment_id=0)

    async def tts_stream(self, text, voice, speed, model):
        self.tts_calls.append(text)

        async def gen():
            yield np.zeros(8, dtype=np.float32)

        return gen(), 24000, {"node": self.node, "model": "fake-tts"}, {}


class _FakeWS:
    """假 WebSocket: 首帧 start, 然后喂一帧 PCM bytes; assistant_done 下发后才放行断开。"""

    def __init__(self, start: dict, pcm: bytes) -> None:
        self._start = start
        self._events = deque([{"type": "websocket.receive", "bytes": pcm}])
        self.sent: list[dict] = []
        self.audio: list[bytes] = []
        self.closed = False
        self._turn_done_evt: asyncio.Event | None = None

    @property
    def _turn_done(self) -> asyncio.Event:
        # 延迟创建: Py3.9 Event 构造时绑 loop, 须在运行中的 loop 内首次访问。
        if self._turn_done_evt is None:
            self._turn_done_evt = asyncio.Event()
        return self._turn_done_evt

    async def receive_json(self):
        return self._start

    async def receive(self):
        if self._events:
            return self._events.popleft()
        # 脚本帧耗尽: 等一轮对话完成后再断开, 保证完整跑过 respond
        await self._turn_done.wait()
        return {"type": "websocket.disconnect"}

    async def send_json(self, obj):
        self.sent.append(obj)
        if obj.get("type") == "assistant_done":
            self._turn_done.set()

    async def send_bytes(self, b):
        self.audio.append(b)

    async def close(self):
        self.closed = True


def _settings() -> Settings:
    return Settings(
        agent_system_prompt="测试人设",
        agent_max_turns=8,
        agent_first_token_timeout=5.0,
        tts_max_sentence_chars=60,
        chat_tts_sentence_stream=True,
    )


def _types(ws: _FakeWS) -> list[str]:
    return [m.get("type") for m in ws.sent]


def test_full_turn_runs_state_machine():
    disp = _FakeDispatcher(final_text="讲个笑话")
    agent = _FakeAgent(tokens=["你好", "，", "今天天气不错。", "再见！"])
    pcm = np.zeros(160, dtype=np.float32).tobytes()
    ws = _FakeWS({"type": "start", "sample_rate": 16000, "channels": 1}, pcm)
    orch = ConversationOrchestrator(disp, agent, _settings())

    async def main():
        await asyncio.wait_for(orch.run(ws), timeout=5.0)

    asyncio.run(main())

    types = _types(ws)
    assert types[0] == "ready"
    assert "user_partial" in types
    assert "user_final" in types
    assert "assistant_partial" in types
    assert "tts_meta" in types
    assert "assistant_done" in types

    # 状态机经历 listening -> thinking -> responding
    states = [m["state"] for m in ws.sent if m.get("type") == "state"]
    assert "listening" in states
    assert "thinking" in states
    assert "responding" in states

    # thinking 必须在 responding 之前
    assert states.index("thinking") < states.index("responding")

    # user_final 回显 ASR 整句
    user_final = next(m for m in ws.sent if m.get("type") == "user_final")
    assert user_final["text"] == "讲个笑话"

    # assistant_done 文本为全部 token 拼接, 且带 QoS 三段时延
    done = next(m for m in ws.sent if m.get("type") == "assistant_done")
    assert done["text"] == "你好，今天天气不错。再见！"
    assert done["qos"]["first_token_ms"] is not None
    assert done["qos"]["tts_ttfb_ms"] is not None
    assert done["qos"]["total_ms"] is not None
    assert done["node"] == "node-test"

    # 按句流式 TTS: 至少合成了一次, 且拼接还原全文
    assert disp.tts_calls
    assert "".join(disp.tts_calls) == "你好，今天天气不错。再见！"
    assert ws.audio  # 有音频字节下发

    # 远端收到 system + user 消息
    assert agent.seen_messages[0]["role"] == "system"
    assert agent.seen_messages[-1] == {"role": "user", "content": "讲个笑话"}

    assert ws.closed is True


def test_unavailable_when_agent_not_configured():
    class _Unconfigured(_FakeAgent):
        @property
        def configured(self) -> bool:
            return False

    disp = _FakeDispatcher(final_text="x")
    agent = _Unconfigured(tokens=[])
    pcm = np.zeros(160, dtype=np.float32).tobytes()
    ws = _FakeWS({"type": "start"}, pcm)
    orch = ConversationOrchestrator(disp, agent, _settings())

    asyncio.run(asyncio.wait_for(orch.run(ws), timeout=5.0))

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "unavailable"
    assert ws.closed is True


def test_pop_complete_keeps_incomplete_tail():
    orch = ConversationOrchestrator(_FakeDispatcher("x"), _FakeAgent([]), _settings())
    # 不以主断句标点结尾: 末段保留
    complete, tail = orch._pop_complete("你好。今天")
    assert complete == ["你好。"]
    assert tail == "今天"
    # 以标点结尾: 全部 flush
    complete, tail = orch._pop_complete("你好。今天天气不错！")
    assert complete == ["你好。", "今天天气不错！"]
    assert tail == ""


def test_truncate_keeps_system_and_recent_turns():
    s = Settings(agent_max_turns=2)
    orch = ConversationOrchestrator(_FakeDispatcher("x"), _FakeAgent([]), s)
    orch._messages = [{"role": "system", "content": "sys"}]
    for i in range(5):
        orch._messages.append({"role": "user", "content": f"u{i}"})
        orch._messages.append({"role": "assistant", "content": f"a{i}"})
    orch._truncate()
    assert orch._messages[0]["role"] == "system"
    # 保留最近 2 轮 = 4 条 + system
    assert len(orch._messages) == 5
    assert orch._messages[-1] == {"role": "assistant", "content": "a4"}
    assert orch._messages[1] == {"role": "user", "content": "u3"}


class _SlowAgent(_FakeAgent):
    """按 token 逐个产出, 每个之间 sleep, 给 barge watcher 留出触发窗口。"""

    async def stream_chat(self, messages):
        self.seen_messages = list(messages)
        for tok in self._tokens:
            await asyncio.sleep(0.03)
            yield tok


class _BargeWS(_FakeWS):
    """持续投递麦克风 PCM 帧, 直到看到 interrupted 帧才放行断开。

    responding 阶段这些帧被路由到 _barge_q, 供 watcher 检测插话。
    """

    def __init__(self, start: dict, pcm: bytes) -> None:
        super().__init__(start, pcm)
        self._pcm_bytes = pcm

    async def receive(self):
        if self._turn_done.is_set():
            return {"type": "websocket.disconnect"}
        await asyncio.sleep(0.005)
        return {"type": "websocket.receive", "bytes": self._pcm_bytes}

    async def send_json(self, obj):
        self.sent.append(obj)
        if obj.get("type") == "interrupted":
            self._turn_done.set()


def _barge_settings() -> Settings:
    return Settings(
        agent_system_prompt="测试人设",
        agent_first_token_timeout=5.0,
        tts_max_sentence_chars=60,
        chat_tts_sentence_stream=True,
        chat_barge_in=True,
        chat_barge_in_min_chars=2,
    )


def test_barge_in_interrupts_response():
    # 无标点单字 token: 全程缓冲不触发 speak, 停在 deltas 循环让 watcher 有时间打断。
    disp = _FakeDispatcher(final_text="讲个笑话")
    agent = _SlowAgent(tokens=list("你好呢今天气不错哦啊"))
    pcm = np.zeros(160, dtype=np.float32).tobytes()
    ws = _BargeWS({"type": "start", "sample_rate": 16000, "channels": 1}, pcm)
    orch = ConversationOrchestrator(disp, agent, _barge_settings())

    asyncio.run(asyncio.wait_for(orch.run(ws), timeout=5.0))

    types = _types(ws)
    # 被打断: 下发 interrupted, 且本轮不发 assistant_done
    assert "interrupted" in types
    assert "assistant_done" not in types
    # Agent 未产完所有 token 就被中止 (打断早于 token 耗尽)
    assert len(agent._tokens) == 10
    done = next((m for m in ws.sent if m.get("type") == "assistant_done"), None)
    assert done is None
    assert ws.closed is True


def test_barge_in_off_completes_turn_despite_audio():
    # barge-in 关 (默认): responding 期间的音频被丢弃, 整轮正常完成。
    disp = _FakeDispatcher(final_text="讲个笑话")
    agent = _SlowAgent(tokens=["你好，", "再见！"])
    pcm = np.zeros(160, dtype=np.float32).tobytes()
    # 用普通 _FakeWS: 只投一帧 PCM, 不持续灌音频
    ws = _FakeWS({"type": "start", "sample_rate": 16000, "channels": 1}, pcm)
    s = _settings()  # chat_barge_in 默认 False
    orch = ConversationOrchestrator(disp, agent, s)

    asyncio.run(asyncio.wait_for(orch.run(ws), timeout=5.0))

    types = _types(ws)
    assert "assistant_done" in types
    assert "interrupted" not in types


def test_start_frame_barge_in_overrides_settings():
    # 前端 start 帧显式传 barge_in, 覆盖 env 默认 (两个方向都覆盖)。
    disp = _FakeDispatcher("x")

    # settings 默认关, start 传 True -> 开
    orch_on = ConversationOrchestrator(disp, _FakeAgent([]), _settings())
    assert orch_on._barge_on is False
    ws_on = _FakeWS(
        {"type": "start", "sample_rate": 16000, "channels": 1, "barge_in": True},
        np.zeros(160, dtype=np.float32).tobytes(),
    )
    asyncio.run(asyncio.wait_for(orch_on.run(ws_on), timeout=5.0))
    assert orch_on._barge_on is True

    # settings 默认开, start 传 False -> 关
    orch_off = ConversationOrchestrator(disp, _FakeAgent([]), _barge_settings())
    assert orch_off._barge_on is True
    ws_off = _FakeWS(
        {"type": "start", "sample_rate": 16000, "channels": 1, "barge_in": False},
        np.zeros(160, dtype=np.float32).tobytes(),
    )
    asyncio.run(asyncio.wait_for(orch_off.run(ws_off), timeout=5.0))
    assert orch_off._barge_on is False
