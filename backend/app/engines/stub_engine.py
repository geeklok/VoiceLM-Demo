from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

import numpy as np

from app.engines.base import ASREngine, ASRPartial, ASRResult, TTSEngine


class StubASREngine(ASREngine):
    """本地开发用 ASR 占位引擎: 不依赖 GPU/模型, 返回基于音频时长的伪文本。

    用于打通完整链路 (预处理 → 引擎 → 后处理 → API → 前端) 与无 GPU 联调。
    """

    name = "stub-asr"
    expected_sample_rate = 16000
    expected_channels = 1

    async def transcribe(
        self, pcm: np.ndarray, language: str = "auto", hotwords: Optional[list[str]] = None
    ) -> ASRResult:
        dur_s = len(pcm) / self.expected_sample_rate
        text = f"[stub-asr] 收到 {dur_s:.2f}s 音频 (lang={language})"
        return ASRResult(text=text, segments=[{"text": text, "start_ms": 0, "end_ms": int(dur_s * 1000)}])

    async def transcribe_stream(
        self, chunks: AsyncIterator[np.ndarray], language: str = "auto",
        hotwords: Optional[list[str]] = None,
    ) -> AsyncIterator[ASRPartial]:
        total = 0
        seg = 0
        async for chunk in chunks:
            total += len(chunk)
            secs = total / self.expected_sample_rate
            yield ASRPartial(text=f"[stub-asr] 累计 {secs:.1f}s ...", is_final=False, segment_id=seg)
        secs = total / self.expected_sample_rate
        yield ASRPartial(text=f"[stub-asr] 转写完成, 共 {secs:.2f}s", is_final=True, segment_id=seg)


class StubTTSEngine(TTSEngine):
    """本地开发用 TTS 占位引擎: 用正弦波模拟语音, 时长与文本长度相关。"""

    name = "stub-tts"
    output_sample_rate = 24000
    voices = ["中文女", "中文男"]

    def _make_tone(self, text: str, speed: float) -> np.ndarray:
        dur_s = max(0.5, len(text) * 0.18 / max(speed, 0.1))
        t = np.linspace(0, dur_s, int(self.output_sample_rate * dur_s), endpoint=False)
        freq = 220.0
        wave = 0.2 * np.sin(2 * np.pi * freq * t).astype(np.float32)
        envelope = np.minimum(1.0, np.minimum(t * 8, (dur_s - t) * 8)).astype(np.float32)
        return (wave * envelope).astype(np.float32)

    async def synthesize(self, text: str, voice: str = "中文女", speed: float = 1.0) -> np.ndarray:
        return self._make_tone(text, speed)

    async def synthesize_stream(
        self, text: str, voice: str = "中文女", speed: float = 1.0
    ) -> AsyncIterator[np.ndarray]:
        pcm = self._make_tone(text, speed)
        chunk = int(self.output_sample_rate * 0.2)  # 200ms 块
        for i in range(0, len(pcm), chunk):
            await asyncio.sleep(0.05)
            yield pcm[i : i + chunk]
