from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Optional

import numpy as np
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.config import Settings
from app.engines.agent_client import AgentClient, AgentError
from app.orchestration.dispatcher import Dispatcher
from app.orchestration.limiter import ConcurrencyLimitError
from app.postprocess.audio_encode import pcm_to_int16_bytes
from app.utils.logging import get_logger
from app.utils.text_split import ends_with_sentence_punct, split_sentences

logger = get_logger(__name__)


class ConversationOrchestrator:
    """L2: 单条 /ws/chat 连接的语音对话状态机 (speech-to-speech)。

    一个连接 = 一段多轮对话。turn 状态机:
      LISTENING(持续录音 + 流式 ASR) --VAD 句末--> THINKING(等 Agent 首 token)
      --首 token--> RESPONDING(token 按句流式 TTS 播放) --播放完--> LISTENING。

    复用 Dispatcher 的 asr_stream / tts_stream (含 GpuLimiter 限流), 复用 AgentClient
    出网 (不占 GPU)。v1 不做 barge-in: RESPONDING/THINKING 阶段丢弃麦克风 PCM, 规避回声。
    """

    def __init__(
        self, dispatcher: Dispatcher, agent_client: AgentClient, settings: Settings
    ) -> None:
        self._disp = dispatcher
        self._agent = agent_client
        self._s = settings
        self._messages: list[dict] = [
            {"role": "system", "content": settings.agent_system_prompt}
        ]
        self._state = "listening"
        # Queue/Event 延迟到 run() (运行中的 loop) 内创建: Py3.9 构造时会绑 loop,
        # 在无运行 loop 的上下文 (如同步构造) 提前创建会抛 RuntimeError。
        self._pcm_q: Optional[asyncio.Queue] = None
        self._stop: Optional[asyncio.Event] = None
        # 连接级参数 (首帧 start 可覆盖)
        self._sample_rate = 16000
        self._channels = 1
        self._language = "auto"
        self._asr_model: Optional[str] = settings.agent_asr_model or None
        self._voice = settings.agent_tts_voice
        self._speed = settings.agent_tts_speed

    @property
    def _node(self) -> str:
        return self._disp.node

    # ---- 主循环 ----------------------------------------------------------

    async def run(self, ws: WebSocket) -> None:
        self._pcm_q = asyncio.Queue()
        self._stop = asyncio.Event()
        if not self._agent.configured:
            await self._safe_send(
                ws, {"type": "error", "code": "unavailable", "message": "语音聊天未启用"}
            )
            await self._safe_close(ws)
            return

        # 首帧: start 配置 (可选; 缺省用默认)
        try:
            start = await ws.receive_json()
        except (WebSocketDisconnect, json.JSONDecodeError):
            await self._safe_close(ws)
            return
        if isinstance(start, dict) and start.get("type") == "start":
            self._sample_rate = int(start.get("sample_rate", 16000))
            self._channels = int(start.get("channels", 1))
            self._language = start.get("language", "auto")
            if start.get("system_prompt"):
                self._messages[0]["content"] = str(start["system_prompt"])
            if start.get("voice"):
                self._voice = str(start["voice"])
            if start.get("speed"):
                self._speed = float(start["speed"])

        reader = asyncio.create_task(self._read_loop(ws))
        await self._safe_send(ws, {"type": "ready", "node": self._node})
        try:
            while not self._stop.is_set():
                await self._send_state(ws, "listening")
                user_text = await self._listen(ws)
                if self._stop.is_set():
                    break
                if not user_text or not user_text.strip():
                    continue
                await self._send_state(ws, "thinking")
                await self._respond(ws, user_text.strip())
        except WebSocketDisconnect:
            logger.info("chat ws disconnected")
        except Exception:  # noqa: BLE001
            logger.exception("chat orchestrator error")
        finally:
            self._stop.set()
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self._safe_close(ws)

    # ---- 接收循环 (独立 task, 唯一调用 ws.receive 的地方) -------------------

    async def _read_loop(self, ws: WebSocket) -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    # v1 无 barge-in: 仅 LISTENING 阶段收音, 其余丢弃避免回声/抢话。
                    if self._state == "listening":
                        pcm = np.frombuffer(msg["bytes"], dtype=np.float32).copy()
                        await self._pcm_q.put((pcm, self._sample_rate, self._channels))
                elif msg.get("text") is not None:
                    try:
                        ctrl = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    if ctrl.get("type") == "end":
                        break
        except WebSocketDisconnect:
            pass
        finally:
            self._stop.set()
            await self._pcm_q.put(None)  # 解阻塞正在等待的 listener

    # ---- LISTENING: 流式 ASR 直到 VAD 句末 --------------------------------

    async def _listen(self, ws: WebSocket) -> Optional[str]:
        self._state = "listening"
        self._drain_queue()

        async def chunk_iter() -> AsyncIterator[tuple[np.ndarray, int, int]]:
            while True:
                item = await self._pcm_q.get()
                if item is None:  # stop / end 哨兵
                    return
                yield item

        final_text: Optional[str] = None
        try:
            async for partial in self._disp.asr_stream(
                chunk_iter(), language=self._language, model=self._asr_model
            ):
                if partial.is_final:
                    final_text = partial.text
                    await self._safe_send(
                        ws, {"type": "user_final", "text": partial.text, "node": self._node}
                    )
                    break
                await self._safe_send(
                    ws, {"type": "user_partial", "text": partial.text, "node": self._node}
                )
        except ConcurrencyLimitError as exc:
            await self._safe_send(
                ws,
                {"type": "error", "code": "busy", "retry_after": exc.retry_after,
                 "message": str(exc)},
            )
        return final_text

    # ---- THINKING + RESPONDING: Agent 流 → 按句 TTS --------------------------

    async def _respond(self, ws: WebSocket, user_text: str) -> None:
        self._messages.append({"role": "user", "content": user_text})
        self._truncate()

        t_start = time.perf_counter()
        first_token_ms: Optional[int] = None
        tts_ttfb_ms: Optional[int] = None
        assistant_full = ""
        buffer = ""
        meta_sent = False

        async def speak(sentence: str) -> None:
            nonlocal meta_sent, tts_ttfb_ms
            if not sentence.strip():
                return
            stream, sr, meta, _qos = await self._disp.tts_stream(
                sentence, self._voice, self._speed, None
            )
            if not meta_sent:
                await self._safe_send(
                    ws,
                    {"type": "tts_meta", "sample_rate": sr, "format": "pcm_s16le",
                     "node": meta.get("node", ""), "model": meta.get("model", "")},
                )
                meta_sent = True
            async for pcm_chunk in stream:
                if tts_ttfb_ms is None:
                    tts_ttfb_ms = int((time.perf_counter() - t_start) * 1000)
                await ws.send_bytes(pcm_to_int16_bytes(pcm_chunk))

        try:
            agen = self._agent.stream_chat(self._messages).__aiter__()
            # 首 token 单独超时, 防远端挂死。
            try:
                first = await asyncio.wait_for(
                    agen.__anext__(), timeout=self._s.agent_first_token_timeout
                )
            except StopAsyncIteration:
                first = None
            except asyncio.TimeoutError as exc:
                await _aclose(agen)
                raise AgentError("Agent 首 token 超时") from exc

            if first is not None:
                first_token_ms = int((time.perf_counter() - t_start) * 1000)
                self._state = "responding"
                await self._send_state(ws, "responding")

                async def deltas() -> AsyncIterator[str]:
                    yield first
                    async for d in agen:
                        yield d

                async for delta in deltas():
                    assistant_full += delta
                    buffer += delta
                    await self._safe_send(
                        ws,
                        {"type": "assistant_partial", "text": assistant_full, "node": self._node},
                    )
                    complete, buffer = self._pop_complete(buffer)
                    for sent in complete:
                        if self._s.chat_tts_sentence_stream:
                            await speak(sent)
                # flush 末段残余
                if self._s.chat_tts_sentence_stream and buffer.strip():
                    await speak(buffer)
                elif not self._s.chat_tts_sentence_stream and assistant_full.strip():
                    # 关闭按句流式: 整段一次合成。
                    await speak(assistant_full)
        except (AgentError, ConcurrencyLimitError) as exc:
            code = "busy" if isinstance(exc, ConcurrencyLimitError) else "agent"
            await self._safe_send(
                ws, {"type": "error", "code": code, "message": str(exc)}
            )
            self._state = "listening"
            return
        except WebSocketDisconnect:
            raise

        if assistant_full.strip():
            self._messages.append({"role": "assistant", "content": assistant_full})

        total_ms = int((time.perf_counter() - t_start) * 1000)
        await self._safe_send(
            ws,
            {
                "type": "assistant_done",
                "text": assistant_full,
                "node": self._node,
                "qos": {
                    "node": self._node,
                    "first_token_ms": first_token_ms,
                    "tts_ttfb_ms": tts_ttfb_ms,
                    "total_ms": total_ms,
                },
            },
        )
        self._state = "listening"

    # ---- 工具 ------------------------------------------------------------

    def _pop_complete(self, buffer: str) -> tuple[list[str], str]:
        """从增量缓冲区取出已完整的句子, 返回 (完整句列表, 剩余未完句)。

        以主断句标点为界。缓冲区不以标点结尾时, 末段视为未完, 留待后续 token;
        但 split_sentences 会把超长无标点段按 max_chars 硬切, 防缓冲区无限增长。
        """
        segs = split_sentences(buffer, self._s.tts_max_sentence_chars, 0)
        if not segs:
            return [], buffer
        if ends_with_sentence_punct(buffer):
            return segs, ""
        return segs[:-1], segs[-1]

    def _truncate(self) -> None:
        """保留 system + 最近 agent_max_turns 轮 (user/assistant 成对)。"""
        system = self._messages[:1]
        rest = self._messages[1:]
        keep = self._s.agent_max_turns * 2
        if len(rest) > keep:
            rest = rest[-keep:]
        self._messages = system + rest

    def _drain_queue(self) -> None:
        while not self._pcm_q.empty():
            try:
                self._pcm_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _send_state(self, ws: WebSocket, state: str) -> None:
        # 单点切状态: _read_loop 据 self._state 决定是否收音 (THINKING/RESPONDING 丢弃)。
        self._state = state
        await self._safe_send(ws, {"type": "state", "state": state, "node": self._node})

    async def _safe_send(self, ws: WebSocket, obj: dict) -> None:
        try:
            await ws.send_json(obj)
        except (WebSocketDisconnect, RuntimeError):
            self._stop.set()

    async def _safe_close(self, ws: WebSocket) -> None:
        try:
            await ws.close()
        except RuntimeError:
            pass


async def _aclose(agen) -> None:
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001
        pass
