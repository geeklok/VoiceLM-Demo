from __future__ import annotations

import asyncio
import json
import time
import unicodedata
from typing import AsyncIterator, Optional

import numpy as np
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.config import Settings
from app.engines.agent_client import AgentClient, AgentError
from app.observability.metrics import (
    observe_chat_playback_dropped_chunks,
    observe_chat_playback_underrun,
    observe_chat_provider_error,
    observe_chat_session,
    observe_chat_turn,
)
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
        # barge-in (说话打断): 开启后 RESPONDING 期间麦克风 PCM 入 _barge_q, 由 watcher
        # 跑 ASR 检测用户插话; _interrupt 置位即中止当前 Agent 流 + TTS。默认关=半双工。
        self._barge_on = bool(settings.chat_barge_in)
        self._barge_q: Optional[asyncio.Queue] = None
        self._interrupt: Optional[asyncio.Event] = None
        # 连接级参数 (首帧 start 可覆盖)
        self._sample_rate = 16000
        self._channels = 1
        self._language = "auto"
        self._asr_model: Optional[str] = settings.agent_asr_model or None
        self._voice = settings.agent_tts_voice
        self._speed = settings.agent_tts_speed
        # Agent 模型 / 思考模式: 连接级, 首帧 start 可覆盖 (None=用 AgentClient 全局默认)。
        self._agent_model: Optional[str] = None
        self._agent_thinking: Optional[bool] = None
        self._provider = "cascade"
        self._turn_endpoint_ms: Optional[int] = None
        self._turn_input_ms: Optional[int] = None
        self._interrupt_detected_at: Optional[float] = None
        self._client_disconnected = False

    @property
    def _node(self) -> str:
        return self._disp.node

    @property
    def _model_label(self) -> str:
        return self._agent_model or self._s.agent_model or "unknown"

    # ---- 主循环 ----------------------------------------------------------

    async def run(self, ws: WebSocket, start: Optional[dict] = None) -> None:
        self._pcm_q = asyncio.Queue()
        self._stop = asyncio.Event()
        self._barge_q = asyncio.Queue()
        self._interrupt = asyncio.Event()
        if not self._agent.configured:
            observe_chat_session(self._provider, self._model_label, "unavailable")
            await self._safe_send(
                ws, {"type": "error", "code": "unavailable", "message": "语音聊天未启用"}
            )
            await self._safe_close(ws)
            return

        # 路由层可预读首帧用于选择 Provider；直接调用时仍兼容由本类读取。
        if start is None:
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
            # barge-in: 前端可显式选择开/关, 覆盖 env 默认; 未传则用 settings 默认。
            if "barge_in" in start:
                self._barge_on = bool(start["barge_in"])
            # Agent 模型: 仅接受白名单内的值 (防注入未授权/不存在模型名致 404)。
            m = start.get("model")
            if m and m in self._s.agent_model_allowlist:
                self._agent_model = str(m)
            # 思考模式: 前端可显式开/关, 覆盖 env 默认; 未传则用 AgentClient 全局默认。
            if "enable_thinking" in start:
                self._agent_thinking = bool(start["enable_thinking"])

        reader = asyncio.create_task(self._read_loop(ws))
        await self._safe_send(ws, {"type": "ready", "node": self._node})
        session_status = "ok"
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
            session_status = "error"
            logger.exception("chat orchestrator error")
        finally:
            self._stop.set()
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self._safe_close(ws)
            if session_status == "ok" and self._client_disconnected:
                session_status = "disconnected"
            observe_chat_session(self._provider, self._model_label, session_status)

    # ---- 接收循环 (独立 task, 唯一调用 ws.receive 的地方) -------------------

    async def _read_loop(self, ws: WebSocket) -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    self._client_disconnected = True
                    break
                if msg.get("bytes") is not None:
                    if self._state == "listening":
                        pcm = np.frombuffer(msg["bytes"], dtype=np.float32).copy()
                        await self._pcm_q.put((pcm, self._sample_rate, self._channels))
                    elif self._barge_on and self._state == "responding":
                        # barge-in: RESPONDING 期间收音送 watcher 检测插话 (依赖前端 AEC 滤回声)。
                        pcm = np.frombuffer(msg["bytes"], dtype=np.float32).copy()
                        await self._barge_q.put((pcm, self._sample_rate, self._channels))
                    # THINKING 阶段或 barge-in 关闭: 丢弃 (避免回声/抢话)。
                elif msg.get("text") is not None:
                    try:
                        ctrl = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    if ctrl.get("type") == "end":
                        break
                    if (
                        ctrl.get("type") == "cancel_response"
                        and self._state in ("thinking", "responding")
                    ):
                        self._interrupt_detected_at = time.perf_counter()
                        self._interrupt.set()
                        continue
                    if ctrl.get("type") == "client_metric":
                        observe_chat_playback_underrun(
                            self._provider,
                            self._model_label,
                            int(ctrl.get("playback_underruns") or 0),
                        )
                        observe_chat_playback_dropped_chunks(
                            self._provider,
                            self._model_label,
                            int(ctrl.get("dropped_chunks") or 0),
                        )
        except WebSocketDisconnect:
            self._client_disconnected = True
        finally:
            self._stop.set()
            await self._pcm_q.put(None)  # 解阻塞正在等待的 listener
            await self._barge_q.put(None)  # 解阻塞正在等待的 barge watcher

    # ---- LISTENING: 流式 ASR 直到 VAD 句末 --------------------------------

    async def _listen(self, ws: WebSocket) -> Optional[str]:
        self._state = "listening"
        self._drain_queue()

        # 累计送入 ASR 的音频样本数, 用于"有效发言"的时长门控 (ASRPartial 不带时长)。
        fed_samples = 0
        sr_seen = self._sample_rate
        last_chunk_at: Optional[float] = None

        async def chunk_iter() -> AsyncIterator[tuple[np.ndarray, int, int]]:
            nonlocal fed_samples, sr_seen, last_chunk_at
            while True:
                item = await self._pcm_q.get()
                if item is None:  # stop / end 哨兵
                    return
                pcm, sr, _ch = item
                fed_samples += len(pcm)
                sr_seen = sr or sr_seen
                last_chunk_at = time.perf_counter()
                yield item

        final_text: Optional[str] = None
        try:
            async for partial in self._disp.asr_stream(
                chunk_iter(), language=self._language, model=self._asr_model
            ):
                if partial.is_final:
                    seg_ms = int(fed_samples / sr_seen * 1000) if sr_seen else 0
                    if self._is_valid_speech(partial.text, seg_ms):
                        final_text = partial.text
                        self._turn_input_ms = seg_ms
                        self._turn_endpoint_ms = (
                            int((time.perf_counter() - last_chunk_at) * 1000)
                            if last_chunk_at is not None
                            else None
                        )
                        await self._safe_send(
                            ws, {"type": "user_final", "text": partial.text, "node": self._node}
                        )
                        break
                    # 噪声/超短: 丢弃这一"句", 重置计数, 继续听 (不触发 Agent)。
                    logger.info(
                        "drop noise final: chars=%d ms=%d text=%r",
                        _real_char_count(partial.text), seg_ms, partial.text[:20],
                    )
                    fed_samples = 0
                    continue
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

    def _is_valid_speech(self, text: str, seg_ms: int) -> bool:
        """有效发言门控: 定稿实际字数与语音时长都达标才算真发言 (滤环境噪声误触发)。"""
        if _real_char_count(text) < self._s.chat_min_speech_chars:
            return False
        if seg_ms < self._s.chat_min_speech_ms:
            return False
        return True

    # ---- THINKING + RESPONDING: Agent 流 → 按句 TTS --------------------------

    async def _respond(self, ws: WebSocket, user_text: str) -> None:
        self._messages.append({"role": "user", "content": user_text})
        self._truncate()

        t_start = time.perf_counter()
        first_token_ms: Optional[int] = None
        tts_ttfb_ms: Optional[int] = None
        assistant_full = ""
        spoken_full = ""
        buffer = ""
        meta_sent = False
        interrupted = False
        output_samples = 0
        output_sample_rate = 0
        self._interrupt.clear()
        self._interrupt_detected_at = None

        async def speak(sentence: str) -> bool:
            """合成并下发一句; 返回 True 表示中途被打断 (barge-in)。"""
            nonlocal meta_sent, output_sample_rate, output_samples, tts_ttfb_ms
            nonlocal spoken_full
            if not sentence.strip():
                return False
            if self._interrupt.is_set():
                return True
            stream, sr, meta, _qos = await self._disp.tts_stream(
                sentence, self._voice, self._speed, None
            )
            output_sample_rate = sr
            if not meta_sent:
                await self._safe_send(
                    ws,
                    {"type": "tts_meta", "sample_rate": sr, "format": "pcm_s16le",
                     "node": meta.get("node", ""), "model": meta.get("model", "")},
                )
                meta_sent = True
            async for pcm_chunk in stream:
                if self._interrupt.is_set():
                    await _aclose(stream)  # 停 TTS: 关闭生成器, 丢弃剩余音频
                    return True
                if tts_ttfb_ms is None:
                    tts_ttfb_ms = int((time.perf_counter() - t_start) * 1000)
                output_samples += len(pcm_chunk)
                await ws.send_bytes(pcm_to_int16_bytes(pcm_chunk))
            spoken_full += sentence
            return False

        watcher: Optional[asyncio.Task] = None
        agen = self._agent.stream_chat(
            self._messages,
            model=self._agent_model,
            enable_thinking=self._agent_thinking,
        ).__aiter__()
        try:
            # 首 token 等待与用户主动停止并行；停止思考不必等到远端先返回内容。
            first_task = asyncio.create_task(agen.__anext__())
            cancel_task = asyncio.create_task(self._interrupt.wait())
            done, pending = await asyncio.wait(
                {first_task, cancel_task},
                timeout=self._s.agent_first_token_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                raise AgentError("Agent 首 token 超时")
            if cancel_task in done and self._interrupt.is_set():
                interrupted = True
                first_task.cancel()
                await asyncio.gather(first_task, return_exceptions=True)
                first = None
            else:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                try:
                    first = first_task.result()
                except StopAsyncIteration:
                    first = None

            if first is not None:
                first_token_ms = int((time.perf_counter() - t_start) * 1000)
                self._state = "responding"
                await self._send_state(ws, "responding")
                # barge-in: 进入 RESPONDING 才起 watcher (此后 _read_loop 把 PCM 路由到 _barge_q)。
                if self._barge_on:
                    self._drain_barge()
                    watcher = asyncio.create_task(self._watch_interrupt())

                async def deltas() -> AsyncIterator[str]:
                    yield first
                    async for d in agen:
                        yield d

                async for delta in deltas():
                    if self._interrupt.is_set():
                        interrupted = True
                        break
                    assistant_full += delta
                    buffer += delta
                    await self._safe_send(
                        ws,
                        {"type": "assistant_partial", "text": assistant_full, "node": self._node},
                    )
                    complete, buffer = self._pop_complete(buffer)
                    for sent in complete:
                        if self._s.chat_tts_sentence_stream:
                            if await speak(sent):
                                interrupted = True
                                break
                    if interrupted:
                        break
                # flush 末段残余 (未被打断时)
                if not interrupted:
                    if self._s.chat_tts_sentence_stream and buffer.strip():
                        interrupted = await speak(buffer)
                    elif not self._s.chat_tts_sentence_stream and assistant_full.strip():
                        # 关闭按句流式: 整段一次合成。
                        interrupted = await speak(assistant_full)
        except (AgentError, ConcurrencyLimitError) as exc:
            code = "busy" if isinstance(exc, ConcurrencyLimitError) else "agent"
            total_ms = int((time.perf_counter() - t_start) * 1000)
            observe_chat_provider_error(
                self._provider, self._model_label, code
            )
            observe_chat_turn(
                self._provider,
                self._model_label,
                status=code,
                endpoint_ms=self._turn_endpoint_ms,
                first_audio_ms=tts_ttfb_ms,
                total_ms=total_ms,
                input_audio_ms=self._turn_input_ms,
                output_audio_ms=_audio_ms(output_samples, output_sample_rate),
            )
            await self._safe_send(
                ws, {"type": "error", "code": code, "message": str(exc)}
            )
            self._state = "listening"
            return
        except WebSocketDisconnect:
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await _aclose(agen)
            self._drain_barge()

        # 被打断时只记录完整下发完毕的句子，避免模型误以为用户已经听到了后续内容。
        history_text = spoken_full if interrupted else assistant_full
        if history_text.strip():
            self._messages.append({"role": "assistant", "content": history_text})

        if interrupted:
            # 打断: 通知前端停播 + 清字幕, 直接回 LISTENING 接住用户新话 (不发 assistant_done)。
            total_ms = int((time.perf_counter() - t_start) * 1000)
            interrupt_ms = (
                int((time.perf_counter() - self._interrupt_detected_at) * 1000)
                if self._interrupt_detected_at is not None
                else None
            )
            observe_chat_turn(
                self._provider,
                self._model_label,
                status="interrupted",
                endpoint_ms=self._turn_endpoint_ms,
                first_audio_ms=tts_ttfb_ms,
                total_ms=total_ms,
                input_audio_ms=self._turn_input_ms,
                output_audio_ms=_audio_ms(output_samples, output_sample_rate),
                interrupt_ms=interrupt_ms,
            )
            await self._safe_send(
                ws,
                {"type": "interrupted", "text": spoken_full, "node": self._node},
            )
            self._state = "listening"
            return

        total_ms = int((time.perf_counter() - t_start) * 1000)
        observe_chat_turn(
            self._provider,
            self._model_label,
            status="ok",
            endpoint_ms=self._turn_endpoint_ms,
            first_audio_ms=tts_ttfb_ms,
            total_ms=total_ms,
            input_audio_ms=self._turn_input_ms,
            output_audio_ms=_audio_ms(output_samples, output_sample_rate),
        )
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

    # ---- barge-in: RESPONDING 期间检测用户插话 -------------------------------

    async def _watch_interrupt(self) -> None:
        """跑轻量流式 ASR 检测插话; 识别到 >=min_chars 个实际字即置 _interrupt。

        判据用「识别到实际文字」而非纯 VAD 能量: 依赖前端浏览器 AEC 抑制外放回声后,
        残余回声不足以形成有效文字, 从而滤掉自打断。watcher 失败不影响主流程 (最坏=本轮不可打断)。
        """
        async def chunk_iter() -> AsyncIterator[tuple[np.ndarray, int, int]]:
            while True:
                item = await self._barge_q.get()
                if item is None:  # stop / 断开哨兵
                    return
                yield item

        try:
            async for partial in self._disp.asr_stream(
                chunk_iter(), language=self._language, model=self._asr_model
            ):
                text = (partial.text or "")
                if _real_char_count(text) >= self._s.chat_barge_in_min_chars:
                    self._interrupt_detected_at = time.perf_counter()
                    self._interrupt.set()
                    return
        except (ConcurrencyLimitError, Exception):  # noqa: BLE001
            return

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

    def _drain_barge(self) -> None:
        while not self._barge_q.empty():
            try:
                self._barge_q.get_nowait()
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


def _real_char_count(text: str) -> int:
    """统计"实际内容字符"数: 排除空白与标点/符号 (Unicode 类别 P*/S*)。

    用于噪声门控与 barge-in 判据 —— 噪声常被误识别成单个字或纯标点,
    去掉标点/空白后不足阈值即视为非有效发言。
    """
    n = 0
    for c in text or "":
        if c.isspace():
            continue
        cat = unicodedata.category(c)
        if cat[0] in ("P", "S"):  # 标点 / 符号
            continue
        n += 1
    return n


def _audio_ms(samples: int, sample_rate: int) -> Optional[int]:
    if samples <= 0 or sample_rate <= 0:
        return None
    return int(samples / sample_rate * 1000)
