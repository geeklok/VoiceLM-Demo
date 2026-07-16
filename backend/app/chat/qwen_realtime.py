from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
import websockets
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.chat.base import ChatProvider
from app.config import Settings
from app.observability.metrics import (
    observe_chat_playback_dropped_chunks,
    observe_chat_playback_underrun,
    observe_chat_provider_error,
    observe_chat_session,
    observe_chat_turn,
    observe_chat_usage,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _SessionState:
    model: str
    input_format: str
    speech_active: bool = False
    speech_stopped_at: Optional[float] = None
    last_input_at: Optional[float] = None
    endpoint_ms: Optional[int] = None
    response_started_at: Optional[float] = None
    interrupt_detected_at: Optional[float] = None
    first_text_ms: Optional[int] = None
    first_audio_ms: Optional[int] = None
    input_bytes: int = 0
    total_input_bytes: int = 0
    output_bytes: int = 0
    assistant_text: str = ""
    meta_sent: bool = False
    active_response: bool = False
    interrupted: bool = False


class QwenRealtimeProvider(ChatProvider):
    """百炼 Qwen-Omni-Realtime 双向 WebSocket 代理。"""

    name = "qwen-realtime"
    mode = "native"

    def __init__(
        self,
        settings: Settings,
        *,
        connect_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._s = settings
        self._connect_factory = connect_factory or websockets.connect
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_n = max(1, settings.qwen_omni_concurrency)

    @property
    def configured(self) -> bool:
        return bool(
            self._s.qwen_omni_enabled
            and self._s.qwen_omni_endpoint
            and self._s.qwen_omni_api_key
            and self._s.qwen_omni_model
        )

    async def run(self, ws: WebSocket, start: dict) -> None:
        model = self._select_model(start.get("model"))
        if not self.configured:
            observe_chat_session(self.name, model, "unavailable")
            await ws.send_json(
                {
                    "type": "error",
                    "code": "unavailable",
                    "message": "原生语音模型未配置",
                }
            )
            await self._safe_close(ws)
            return

        if self._sem is None:
            self._sem = asyncio.Semaphore(self._sem_n)
        if self._sem.locked():
            observe_chat_session(self.name, model, "busy")
            await ws.send_json(
                {"type": "error", "code": "busy", "message": "原生语音并发已满"}
            )
            await self._safe_close(ws)
            return

        await self._sem.acquire()
        session_status = "ok"
        state = _SessionState(
            model=model,
            input_format=str(start.get("input_format") or "pcm_f32le"),
        )
        try:
            async with self._connect_factory(
                self._model_url(model),
                additional_headers={
                    "Authorization": f"Bearer {self._s.qwen_omni_api_key}"
                },
                open_timeout=self._s.qwen_omni_connect_timeout,
                ping_interval=20,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            ) as upstream:
                await self._bootstrap(upstream, start)
                await ws.send_json(
                    {
                        "type": "ready",
                        "node": self._s.node_name,
                        "provider": self.name,
                        "model": model,
                    }
                )
                await self._send_state(ws, "listening")

                browser_task = asyncio.create_task(
                    self._browser_to_provider(ws, upstream, state)
                )
                provider_task = asyncio.create_task(
                    self._provider_to_browser(ws, upstream, state)
                )
                done, pending = await asyncio.wait(
                    {browser_task, provider_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            session_status = "error"
            logger.exception("qwen realtime session failed")
            observe_chat_provider_error(self.name, model, "connection")
            try:
                await ws.send_json(
                    {
                        "type": "error",
                        "code": "provider",
                        "message": f"原生语音服务连接失败: {exc}",
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                pass
        finally:
            observe_chat_session(self.name, model, session_status)
            self._sem.release()
            await self._safe_close(ws)

    async def _bootstrap(self, upstream: Any, start: dict) -> None:
        created = self._decode_event(
            await asyncio.wait_for(
                upstream.recv(), timeout=self._s.qwen_omni_connect_timeout
            )
        )
        if created.get("type") != "session.created":
            raise RuntimeError(
                f"期望 session.created，实际为 {created.get('type', 'unknown')}"
            )
        await upstream.send(json.dumps(self._session_update(start)))
        while True:
            event = self._decode_event(
                await asyncio.wait_for(
                    upstream.recv(), timeout=self._s.qwen_omni_connect_timeout
                )
            )
            if event.get("type") == "session.updated":
                return
            if event.get("type") == "error":
                error = event.get("error") or {}
                raise RuntimeError(error.get("message") or "session.update 失败")

    async def _browser_to_provider(
        self, ws: WebSocket, upstream: Any, state: _SessionState
    ) -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                pcm = self._to_pcm16(msg["bytes"], state.input_format)
                state.input_bytes += len(pcm)
                state.total_input_bytes += len(pcm)
                state.last_input_at = time.perf_counter()
                await upstream.send(
                    json.dumps(
                        {
                            "event_id": self._event_id(),
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm).decode("ascii"),
                        }
                    )
                )
                continue
            if msg.get("text") is None:
                continue
            try:
                ctrl = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue
            if ctrl.get("type") == "end":
                return
            if ctrl.get("type") == "client_metric":
                underruns = int(ctrl.get("playback_underruns") or 0)
                observe_chat_playback_underrun(
                    self.name, state.model, underruns
                )
                observe_chat_playback_dropped_chunks(
                    self.name,
                    state.model,
                    int(ctrl.get("dropped_chunks") or 0),
                )

    async def _provider_to_browser(
        self, ws: WebSocket, upstream: Any, state: _SessionState
    ) -> None:
        async for raw in upstream:
            event = self._decode_event(raw)
            await self._handle_server_event(ws, event, state)

    async def _handle_server_event(
        self, ws: WebSocket, event: dict, state: _SessionState
    ) -> None:
        event_type = event.get("type", "")
        now = time.perf_counter()

        if event_type == "input_audio_buffer.speech_started":
            state.speech_active = True
            state.input_bytes = 0
            if state.active_response:
                state.interrupted = True
                state.interrupt_detected_at = now
                await ws.send_json(
                    {
                        "type": "interrupted",
                        "node": self._s.node_name,
                        "provider": self.name,
                    }
                )
            await self._send_state(ws, "listening")
            return

        if event_type == "input_audio_buffer.speech_stopped":
            state.speech_active = False
            state.speech_stopped_at = now
            audio_end_ms = event.get("audio_end_ms")
            if isinstance(audio_end_ms, (int, float)):
                sent_audio_ms = self._pcm_ms(state.total_input_bytes, 16000) or 0
                state.endpoint_ms = max(0, sent_audio_ms - int(audio_end_ms))
            else:
                state.endpoint_ms = (
                    int((now - state.last_input_at) * 1000)
                    if state.last_input_at is not None
                    else None
                )
            await self._send_state(ws, "thinking")
            return

        if event_type == "conversation.item.input_audio_transcription.delta":
            text = f"{event.get('text', '')}{event.get('stash', '')}"
            await ws.send_json(
                {
                    "type": "user_partial",
                    "text": text,
                    "emotion": event.get("emotion"),
                    "node": self._s.node_name,
                }
            )
            return

        if event_type == "conversation.item.input_audio_transcription.completed":
            await ws.send_json(
                {
                    "type": "user_final",
                    "text": event.get("transcript", ""),
                    "node": self._s.node_name,
                }
            )
            return

        if event_type == "response.created":
            state.response_started_at = now
            state.first_text_ms = None
            state.first_audio_ms = None
            state.output_bytes = 0
            state.assistant_text = ""
            state.meta_sent = False
            state.active_response = True
            state.interrupted = False
            return

        if event_type in ("response.audio_transcript.delta", "response.text.delta"):
            if state.interrupted:
                return
            if state.first_text_ms is None and state.speech_stopped_at is not None:
                state.first_text_ms = int((now - state.speech_stopped_at) * 1000)
            state.assistant_text += str(event.get("delta") or "")
            await ws.send_json(
                {
                    "type": "assistant_partial",
                    "text": state.assistant_text,
                    "node": self._s.node_name,
                }
            )
            return

        if event_type == "response.audio.delta":
            if state.interrupted:
                return
            try:
                audio = base64.b64decode(event.get("delta") or "", validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("原生语音返回了非法音频数据") from exc
            if not state.meta_sent:
                state.meta_sent = True
                await ws.send_json(
                    {
                        "type": "tts_meta",
                        "sample_rate": 24000,
                        "format": "pcm_s16le",
                        "node": self._s.node_name,
                        "provider": self.name,
                        "model": state.model,
                    }
                )
                await self._send_state(ws, "responding")
            if state.first_audio_ms is None and state.speech_stopped_at is not None:
                state.first_audio_ms = int((now - state.speech_stopped_at) * 1000)
            state.output_bytes += len(audio)
            await ws.send_bytes(audio)
            return

        if event_type == "response.done":
            await self._finish_response(ws, event, state, now)
            return

        if event_type == "error":
            error = event.get("error") or {}
            code = str(error.get("code") or "provider")
            observe_chat_provider_error(self.name, state.model, code)
            await ws.send_json(
                {
                    "type": "error",
                    "code": "provider",
                    "message": error.get("message") or "原生语音服务错误",
                }
            )

    async def _finish_response(
        self, ws: WebSocket, event: dict, state: _SessionState, now: float
    ) -> None:
        response = event.get("response") or {}
        observe_chat_usage(self.name, state.model, response.get("usage"))
        response_status = str(response.get("status") or "completed")
        total_ms = (
            int((now - state.speech_stopped_at) * 1000)
            if state.speech_stopped_at is not None
            else None
        )
        interrupt_ms = (
            int((now - state.interrupt_detected_at) * 1000)
            if state.interrupted and state.interrupt_detected_at is not None
            else None
        )
        status = "interrupted" if state.interrupted else (
            "ok" if response_status == "completed" else response_status
        )
        observe_chat_turn(
            self.name,
            state.model,
            status=status,
            endpoint_ms=state.endpoint_ms,
            first_audio_ms=state.first_audio_ms,
            total_ms=total_ms,
            input_audio_ms=self._pcm_ms(state.input_bytes, 16000),
            output_audio_ms=self._pcm_ms(state.output_bytes, 24000),
            interrupt_ms=interrupt_ms,
        )
        if not state.interrupted:
            await ws.send_json(
                {
                    "type": "assistant_done",
                    "text": state.assistant_text,
                    "node": self._s.node_name,
                    "provider": self.name,
                    "qos": {
                        "node": self._s.node_name,
                        "first_token_ms": state.first_text_ms,
                        "tts_ttfb_ms": state.first_audio_ms,
                        "total_ms": total_ms,
                        "usage": response.get("usage"),
                    },
                }
            )
            await self._send_state(ws, "listening")
        state.active_response = False
        state.response_started_at = None
        state.interrupt_detected_at = None
        state.endpoint_ms = None
        state.output_bytes = 0
        state.assistant_text = ""
        if not state.speech_active:
            state.input_bytes = 0

    def _session_update(self, start: dict) -> dict:
        requested_voice = str(start.get("voice") or "")
        voice = (
            requested_voice
            if requested_voice in self._s.qwen_omni_voices
            else self._s.qwen_omni_default_voice
        )
        instructions = str(
            start.get("system_prompt") or self._s.qwen_omni_system_prompt
        )[:4000]
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "voice": voice,
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "instructions": instructions,
            "turn_detection": {
                "type": self._s.qwen_omni_turn_detection,
                "threshold": self._s.qwen_omni_vad_threshold,
                "silence_duration_ms": self._s.qwen_omni_silence_ms,
            },
            "temperature": self._s.qwen_omni_temperature,
            "max_tokens": self._s.qwen_omni_max_tokens,
        }
        if self._s.qwen_omni_input_transcription:
            session["input_audio_transcription"] = {
                "model": "qwen3-asr-flash-realtime"
            }
        return {
            "event_id": self._event_id(),
            "type": "session.update",
            "session": session,
        }

    def _select_model(self, requested: Any) -> str:
        model = str(requested or "")
        allowed = set(self._s.qwen_omni_model_allowlist)
        if model and model in allowed:
            return model
        return self._s.qwen_omni_model

    def _model_url(self, model: str) -> str:
        parts = urlsplit(self._s.qwen_omni_endpoint)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["model"] = model
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def _send_state(self, ws: WebSocket, state: str) -> None:
        await ws.send_json(
            {
                "type": "state",
                "state": state,
                "node": self._s.node_name,
                "provider": self.name,
            }
        )

    @staticmethod
    def _decode_event(raw: Any) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise RuntimeError("原生语音服务返回了非对象事件")
        return event

    @staticmethod
    def _to_pcm16(data: bytes, input_format: str) -> bytes:
        if input_format == "pcm_s16le":
            if len(data) % 2:
                raise ValueError("pcm_s16le 音频字节数必须为偶数")
            return data
        if input_format != "pcm_f32le":
            raise ValueError(f"不支持的输入音频格式: {input_format}")
        if len(data) % 4:
            raise ValueError("pcm_f32le 音频字节数必须为 4 的倍数")
        pcm = np.frombuffer(data, dtype="<f4")
        return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    @staticmethod
    def _pcm_ms(n_bytes: int, sample_rate: int) -> Optional[int]:
        if n_bytes <= 0 or sample_rate <= 0:
            return None
        return int(n_bytes / 2 / sample_rate * 1000)

    @staticmethod
    def _event_id() -> str:
        return "event_" + uuid.uuid4().hex

    @staticmethod
    async def _safe_close(ws: WebSocket) -> None:
        try:
            await ws.close()
        except RuntimeError:
            pass
