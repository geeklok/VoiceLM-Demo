from __future__ import annotations

import asyncio
import base64
import json

import numpy as np

from app.api.routes_chat import build_chat_model_catalog
from app.chat.qwen_realtime import QwenRealtimeProvider, _SessionState
from app.config import Settings
from app.engines.agent_client import AgentClient
from app.engines.registry import EngineRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.json: list[dict] = []
        self.audio: list[bytes] = []
        self.closed = False

    async def send_json(self, obj: dict) -> None:
        self.json.append(obj)

    async def send_bytes(self, data: bytes) -> None:
        self.audio.append(data)

    async def close(self) -> None:
        self.closed = True


class _FakeUpstream:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _ControlWS(_FakeWS):
    def __init__(self, controls: list[dict]) -> None:
        super().__init__()
        self._controls = iter(controls)

    async def receive(self) -> dict:
        return {
            "type": "websocket.receive",
            "text": json.dumps(next(self._controls)),
        }


def _settings() -> Settings:
    return Settings(
        node_name="node-test",
        qwen_omni_enabled=True,
        qwen_omni_endpoint="wss://workspace.example/api-ws/v1/realtime?foo=bar",
        qwen_omni_api_key="test-key",
        qwen_omni_model="qwen3.5-omni-flash-realtime",
        qwen_omni_model_allowlist=[
            "qwen3.5-omni-flash-realtime",
            "qwen3.5-omni-plus-realtime",
        ],
        qwen_omni_voices=["Tina", "Cherry"],
        qwen_omni_default_voice="Tina",
        qwen_omni_turn_detection="semantic_vad",
        qwen_omni_silence_ms=600,
    )


def test_session_update_and_model_url_are_allowlisted():
    provider = QwenRealtimeProvider(_settings())
    update = provider._session_update(
        {"voice": "Cherry", "system_prompt": "简短回答"}
    )

    assert update["type"] == "session.update"
    assert update["session"]["voice"] == "Cherry"
    assert update["session"]["instructions"] == "简短回答"
    assert update["session"]["turn_detection"] == {
        "type": "semantic_vad",
        "threshold": 0.5,
        "silence_duration_ms": 600,
    }
    assert update["session"]["input_audio_transcription"]["model"] == (
        "qwen3-asr-flash-realtime"
    )
    assert provider._select_model("qwen3.5-omni-plus-realtime") == (
        "qwen3.5-omni-plus-realtime"
    )
    assert provider._select_model("not-allowed") == (
        "qwen3.5-omni-flash-realtime"
    )
    assert provider._model_url("qwen3.5-omni-plus-realtime") == (
        "wss://workspace.example/api-ws/v1/realtime?"
        "foo=bar&model=qwen3.5-omni-plus-realtime"
    )
    fast = provider._session_update({"vad_silence_ms": 800})
    assert fast["session"]["turn_detection"]["silence_duration_ms"] == 800
    invalid = provider._session_update({"vad_silence_ms": 999})
    assert invalid["session"]["turn_detection"]["silence_duration_ms"] == 600


def test_pcm_conversion_supports_native_int16_and_float32():
    pcm16 = np.array([-32768, 0, 32767], dtype="<i2").tobytes()
    assert QwenRealtimeProvider._to_pcm16(pcm16, "pcm_s16le") == pcm16

    f32 = np.array([-1.0, 0.0, 1.0], dtype="<f4").tobytes()
    out = np.frombuffer(
        QwenRealtimeProvider._to_pcm16(f32, "pcm_f32le"), dtype="<i2"
    )
    assert out.tolist() == [-32767, 0, 32767]


def test_server_events_map_to_existing_chat_protocol():
    provider = QwenRealtimeProvider(_settings())
    ws = _FakeWS()
    state = _SessionState(
        model="qwen3.5-omni-flash-realtime",
        input_format="pcm_s16le",
        input_bytes=32000,
    )
    audio = np.zeros(2400, dtype="<i2").tobytes()

    async def run() -> None:
        await provider._handle_server_event(
            ws, {"type": "input_audio_buffer.speech_stopped"}, state
        )
        await provider._handle_server_event(
            ws,
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "text": "你好",
                "stash": "啊",
                "emotion": "happy",
            },
            state,
        )
        await provider._handle_server_event(
            ws,
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "你好啊",
            },
            state,
        )
        await provider._handle_server_event(
            ws, {"type": "response.created"}, state
        )
        await provider._handle_server_event(
            ws,
            {"type": "response.audio_transcript.delta", "delta": "你好！"},
            state,
        )
        await provider._handle_server_event(
            ws,
            {
                "type": "response.audio.delta",
                "delta": base64.b64encode(audio).decode("ascii"),
            },
            state,
        )
        await provider._handle_server_event(
            ws,
            {
                "type": "response.done",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            },
            state,
        )

    asyncio.run(run())

    types = [item["type"] for item in ws.json]
    assert "user_partial" in types
    assert "user_final" in types
    assert "assistant_partial" in types
    assert "tts_meta" in types
    assert "assistant_done" in types
    assert ws.audio == [audio]
    partial = next(item for item in ws.json if item["type"] == "user_partial")
    assert partial["text"] == "你好啊"
    assert partial["emotion"] == "happy"
    done = next(item for item in ws.json if item["type"] == "assistant_done")
    assert done["text"] == "你好！"
    assert done["qos"]["usage"] == {"input_tokens": 10, "output_tokens": 20}


def test_speech_start_interrupts_and_drops_late_audio():
    provider = QwenRealtimeProvider(_settings())
    ws = _FakeWS()
    upstream = _FakeUpstream()
    state = _SessionState(
        model="qwen3.5-omni-flash-realtime",
        input_format="pcm_s16le",
        active_response=True,
    )

    async def run() -> None:
        await provider._handle_server_event(
            ws, {"type": "input_audio_buffer.speech_started"}, state, upstream
        )
        await provider._handle_server_event(
            ws,
            {
                "type": "response.audio.delta",
                "delta": base64.b64encode(b"late-audio").decode("ascii"),
            },
            state,
        )

    asyncio.run(run())

    assert any(item["type"] == "interrupted" for item in ws.json)
    assert ws.audio == []
    assert any(item["type"] == "response.cancel" for item in upstream.sent)


def test_manual_cancel_stops_active_upstream_response():
    provider = QwenRealtimeProvider(_settings())
    ws = _ControlWS([{"type": "cancel_response"}, {"type": "end"}])
    upstream = _FakeUpstream()
    state = _SessionState(
        model="qwen3.5-omni-flash-realtime",
        input_format="pcm_s16le",
        active_response=True,
        assistant_text="已播放内容",
    )

    asyncio.run(provider._browser_to_provider(ws, upstream, state))

    assert any(item["type"] == "response.cancel" for item in upstream.sent)
    interrupted = next(item for item in ws.json if item["type"] == "interrupted")
    assert interrupted["text"] == "已播放内容"
    assert state.interrupted is True


def test_speech_stop_freezes_turn_input_size():
    provider = QwenRealtimeProvider(_settings())
    ws = _FakeWS()
    state = _SessionState(
        model="qwen3.5-omni-flash-realtime",
        input_format="pcm_s16le",
        input_bytes=32000,
    )

    async def run() -> None:
        await provider._handle_server_event(
            ws, {"type": "input_audio_buffer.speech_stopped"}, state
        )
        state.input_bytes += 64000

    asyncio.run(run())
    assert state.turn_input_bytes == 32000


def test_chat_model_catalog_is_driven_by_configured_providers():
    settings = Settings(
        chat_enabled=True,
        agent_endpoint="https://agent.example/v1",
        agent_api_key="agent-key",
        agent_model="qwen-plus",
        agent_model_allowlist=["qwen-plus", "qwen3.7-plus"],
        qwen_omni_enabled=True,
        qwen_omni_endpoint="wss://workspace.example/api-ws/v1/realtime",
        qwen_omni_api_key="omni-key",
        qwen_omni_model="qwen3.5-omni-flash-realtime",
        qwen_omni_model_allowlist=["qwen3.5-omni-flash-realtime"],
        qwen_omni_voices=["Tina"],
    )
    catalog = build_chat_model_catalog(
        settings,
        EngineRegistry(settings),
        AgentClient(settings),
        QwenRealtimeProvider(settings),
    )

    by_name = {item.name: item for item in catalog.models}
    assert set(by_name) == {
        "qwen-plus",
        "qwen3.7-plus",
        "qwen3.5-omni-flash-realtime",
    }
    assert by_name["qwen-plus"].mode == "cascade"
    native = by_name["qwen3.5-omni-flash-realtime"]
    assert native.mode == "native"
    assert native.input_format == "pcm_s16le"
    assert native.preserves_paralinguistics is True
    assert native.supports_vad_gate is False
    assert native.default_capture_profile == "natural"
    assert native.default_barge_in is True
    assert native.vad_silence_ms_options == [800, 1500, 2000]
