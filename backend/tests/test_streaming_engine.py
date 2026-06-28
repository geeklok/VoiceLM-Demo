from __future__ import annotations

import asyncio

import numpy as np

from app.config import Settings
from app.engines.base import ASREngine, ASRResult
from app.engines.funasr_engine import FunASRStreamingEngine


class _FakeOffline(ASREngine):
    """假 offline 引擎: 整句重解码返回固定修正文本。"""

    def __init__(self) -> None:
        self.name = "fake-offline"
        self.expected_sample_rate = 16000
        self.expected_channels = 1
        self.calls = 0

    async def transcribe(self, pcm, language="auto", hotwords=None) -> ASRResult:  # type: ignore[override]
        self.calls += 1
        return ASRResult(text="定稿句")

    async def transcribe_stream(self, chunks, language="auto"):  # type: ignore[override]
        yield  # pragma: no cover


class _FakeStreamModel:
    """假 paraformer 流式: 每次 generate 返回一个增量字。"""

    def __init__(self) -> None:
        self._i = 0

    def generate(self, **kwargs):
        self._i += 1
        return [{"text": f"临{self._i}"}]


class _FakeVad:
    """假 fsmn-vad: 第 2 次调用报告句子结束 (end != -1)。"""

    def __init__(self) -> None:
        self._i = 0

    def generate(self, **kwargs):
        self._i += 1
        if self._i == 2:
            return [{"value": [[0, 1200]]}]
        return [{"value": [[0, -1]]}]


async def _drain(engine: FunASRStreamingEngine, n_chunks: int):
    async def _chunks():
        # 每块 0.3s => 2 块凑满一个 600ms 聚合窗。
        block = np.zeros(int(16000 * 0.3), dtype=np.float32)
        for _ in range(n_chunks):
            yield block

    out = []
    async for p in engine.transcribe_stream(_chunks()):
        out.append(p)
    return out


def _make_engine() -> tuple[FunASRStreamingEngine, _FakeOffline]:
    settings = Settings(asr_engine="stub", tts_engine="stub", funasr_streaming_chunk_ms=600)
    offline = _FakeOffline()
    eng = FunASRStreamingEngine(settings, name="funasr-streaming", offline_engine=offline)
    # 注入假模型, 跳过真实 _load (避免 funasr 依赖)。
    eng._asr = _FakeStreamModel()  # type: ignore[attr-defined]
    eng._vad = _FakeVad()  # type: ignore[attr-defined]
    eng._load = lambda: None  # type: ignore[assignment]
    return eng, offline


def test_streaming_emits_partial_then_corrected_final():
    eng, offline = _make_engine()
    # 6 块 * 0.3s => 3 个聚合窗 + 收尾; 第 2 窗 vad 报句末 -> 触发 2pass 修正。
    out = asyncio.run(_drain(eng, 6))

    partials = [p for p in out if not p.is_final]
    finals = [p for p in out if p.is_final]
    assert partials, "应有流式临时 partial"
    assert finals, "应有定稿 final"
    # 句末 final 来自 offline 修正。
    assert any(p.text == "定稿句" for p in finals)
    assert offline.calls >= 1


def test_streaming_segment_id_increments():
    eng, _ = _make_engine()
    out = asyncio.run(_drain(eng, 6))
    # 句末 final 后 seg_id 递增; 收尾再出一个 final。
    finals = [p for p in out if p.is_final]
    seg_ids = [p.segment_id for p in finals]
    assert seg_ids == sorted(seg_ids)
    assert seg_ids[-1] >= 1


def test_transcribe_delegates_to_offline():
    eng, offline = _make_engine()
    res = asyncio.run(eng.transcribe(np.zeros(16000, dtype=np.float32)))
    assert res.text == "定稿句"
    assert offline.calls == 1
