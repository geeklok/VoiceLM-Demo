from __future__ import annotations

import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _wav_bytes(sample_rate: int = 16000, seconds: float = 0.5) -> bytes:
    n = int(sample_rate * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    sig = (0.4 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(sig.tobytes())
    return buf.getvalue()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models(client):
    r = client.get("/api/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert len(body["asr"]) == 1
    assert len(body["tts"]) == 1


def test_tn_categories(client):
    r = client.get("/api/v1/tn-categories")
    assert r.status_code == 200
    body = r.json()
    # 与后端 DOMAIN_TN_CATEGORIES 单一事实来源一致 (前端不再硬编码)。
    from app.utils.domain_tn import DOMAIN_TN_CATEGORIES

    assert len(body) == len(DOMAIN_TN_CATEGORIES)
    by_id = {c["id"]: c for c in body}
    for key, (impl, label) in DOMAIN_TN_CATEGORIES.items():
        assert by_id[key]["impl"] is impl
        assert by_id[key]["label"] == label
    # 已实现类别至少含新转正的 datetime/finance/abbrev。
    impl_ids = {c["id"] for c in body if c["impl"]}
    assert {"datetime", "finance", "abbrev"} <= impl_ids


def test_asr_file_stub(client):
    files = {"file": ("test.wav", _wav_bytes(), "audio/wav")}
    r = client.post("/api/v1/asr", files=files, data={"language": "zh"})
    assert r.status_code == 200
    body = r.json()
    assert "stub-asr" in body["text"]
    assert body["audio_duration_ms"] > 0


def test_tts_file_stub(client):
    r = client.post("/api/v1/tts", json={"text": "你好世界", "voice": "中文女"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert len(r.content) > 44  # WAV header + data


def test_ws_tts_stream(client):
    with client.websocket_connect("/ws/tts") as ws:
        ws.send_json({"type": "synthesize", "text": "你好", "voice": "中文女"})
        meta = ws.receive_json()
        assert meta["type"] == "meta"
        got_audio = False
        while True:
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"]:
                got_audio = True
            elif "text" in msg and msg["text"]:
                import json

                if json.loads(msg["text"]).get("type") == "done":
                    break
        assert got_audio


def test_ws_asr_stream(client):
    with client.websocket_connect("/ws/asr") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "channels": 1})
        pcm = np.zeros(16000, dtype=np.float32)
        ws.send_bytes(pcm.tobytes())
        ws.send_json({"type": "end"})
        saw_final = False
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "final":
                saw_final = True
                break
        assert saw_final
