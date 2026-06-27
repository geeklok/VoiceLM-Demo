from __future__ import annotations

import asyncio
import io
import wave

import numpy as np

from app.engines.base import ASREngine, ASRPartial, ASRResult
from app.orchestration.breaker import CircuitBreaker
from app.orchestration.dispatcher import Dispatcher
from app.orchestration.limiter import GpuLimiter


def _wav_bytes(sample_rate: int = 16000, seconds: float = 0.3) -> bytes:
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


class _ScriptedASR(ASREngine):
    """可编排成功/失败的假引擎, 用于验证降级链与熔断。"""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.expected_sample_rate = 16000
        self.expected_channels = 1
        self.languages = ["zh"]
        self.fail = fail
        self.calls = 0

    async def transcribe(self, pcm, language="auto", hotwords=None) -> ASRResult:  # type: ignore[override]
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} boom")
        return ASRResult(text=self.name, segments=[])

    async def transcribe_stream(self, chunks, language="auto"):  # type: ignore[override]
        yield ASRPartial(text=self.name, is_final=True)


class _Registry:
    """最小注册表: 复用真实 EngineRegistry.asr/asr_chain 的语义。"""

    def __init__(self, engines: list[_ScriptedASR]) -> None:
        self._asr = {e.name: e for e in engines}
        self._default = engines[0].name

    def asr(self, name=None):
        if name and name in self._asr:
            return self._asr[name]
        return self._asr[self._default]

    def asr_chain(self, name=None):
        primary = self.asr(name)
        chain = [primary]
        for e in self._asr.values():
            if e.name != primary.name:
                chain.append(e)
        return chain


def test_no_fallback_when_primary_ok():
    a = _ScriptedASR("a")
    b = _ScriptedASR("b")
    disp = Dispatcher(_Registry([a, b]), GpuLimiter(), CircuitBreaker())
    resp = asyncio.run(disp.asr_file(_wav_bytes()))
    assert resp.model == "a"
    assert resp.degraded is False
    assert b.calls == 0


def test_fallback_to_next_on_failure():
    a = _ScriptedASR("a", fail=True)
    b = _ScriptedASR("b")
    disp = Dispatcher(_Registry([a, b]), GpuLimiter(), CircuitBreaker())
    resp = asyncio.run(disp.asr_file(_wav_bytes()))
    assert resp.model == "b"
    assert resp.degraded is True
    assert a.calls == 1 and b.calls == 1


def test_all_fail_raises_last_exception():
    a = _ScriptedASR("a", fail=True)
    b = _ScriptedASR("b", fail=True)
    disp = Dispatcher(_Registry([a, b]), GpuLimiter(), CircuitBreaker())
    try:
        asyncio.run(disp.asr_file(_wav_bytes()))
        assert False, "should have raised"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_breaker_opens_and_skips_engine():
    a = _ScriptedASR("a", fail=True)
    b = _ScriptedASR("b")
    breaker = CircuitBreaker(fail_threshold=2, cooldown=60.0)
    disp = Dispatcher(_Registry([a, b]), GpuLimiter(), breaker)

    # 第 1 次: a 失败计 1, 降级到 b
    asyncio.run(disp.asr_file(_wav_bytes()))
    # 第 2 次: a 失败计 2 -> 熔断打开
    asyncio.run(disp.asr_file(_wav_bytes()))
    assert breaker.is_open("a")
    a.calls = 0
    # 第 3 次: a 已熔断, 直接跳过, 不再调用 a
    resp = asyncio.run(disp.asr_file(_wav_bytes()))
    assert a.calls == 0
    assert resp.model == "b"
    assert resp.degraded is True


def test_fallback_disabled_raises_without_trying_next():
    a = _ScriptedASR("a", fail=True)
    b = _ScriptedASR("b")
    disp = Dispatcher(
        _Registry([a, b]), GpuLimiter(), CircuitBreaker(), asr_fallback_enabled=False
    )
    try:
        asyncio.run(disp.asr_file(_wav_bytes()))
        assert False, "should have raised"
    except RuntimeError:
        pass
    assert b.calls == 0


def test_infer_timeout_triggers_fallback():
    class _Slow(_ScriptedASR):
        async def transcribe(self, pcm, language="auto", hotwords=None):  # type: ignore[override]
            self.calls += 1
            await asyncio.sleep(1.0)
            return ASRResult(text=self.name)

    slow = _Slow("slow")
    fast = _ScriptedASR("fast")
    disp = Dispatcher(
        _Registry([slow, fast]),
        GpuLimiter(),
        CircuitBreaker(),
        asr_infer_timeout=0.05,
    )
    resp = asyncio.run(disp.asr_file(_wav_bytes()))
    assert resp.model == "fast"
    assert resp.degraded is True
