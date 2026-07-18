from __future__ import annotations

import asyncio
import time

import numpy as np

from app.config import Settings
from app.engines.cosyvoice_engine import CosyVoiceEngine
from app.utils.errors import UnknownVoiceError


class _Speech:
    def __init__(self, value: int) -> None:
        self._pcm = np.full(16, value, dtype=np.float32)

    def numpy(self) -> np.ndarray:
        return self._pcm


def test_unknown_voice_is_rejected() -> None:
    engine = CosyVoiceEngine(
        Settings(cosyvoice_default_voice="voice-a")
    )
    engine._model = object()  # type: ignore[attr-defined]
    engine._voices = ["voice-a"]  # type: ignore[attr-defined]

    try:
        engine._resolve_voice("voice-b")  # type: ignore[attr-defined]
        assert False, "unknown voice should be rejected"
    except UnknownVoiceError:
        pass


def test_stream_close_stops_background_producer() -> None:
    engine = CosyVoiceEngine(
        Settings(
            cosyvoice_default_voice="voice-a",
            tts_stream_queue_chunks=1,
        )
    )
    engine._model = object()  # type: ignore[attr-defined]
    engine._voices = ["voice-a"]  # type: ignore[attr-defined]
    produced = {"count": 0}

    def generate(_text: str, _voice: str, _speed: float, stream: bool):
        assert stream is True
        for i in range(100):
            produced["count"] += 1
            time.sleep(0.002)
            yield {"tts_speech": _Speech(i)}

    engine._generate = generate  # type: ignore[method-assign]

    async def run() -> None:
        stream = engine.synthesize_stream("长文本", "voice-a")
        first = await stream.__anext__()
        assert first.size == 16
        await stream.aclose()

    asyncio.run(run())
    # 有界队列最多允许少量预取；关闭消费端后不应继续跑完整 100 块。
    assert produced["count"] < 10
