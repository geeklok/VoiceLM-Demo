from __future__ import annotations

from app.config import Settings
from app.engines.base import ASREngine, ASRPartial, ASRResult
from app.engines.registry import EngineRegistry


class _FakeASR(ASREngine):
    def __init__(self, name: str) -> None:
        self.name = name
        self.expected_sample_rate = 16000
        self.expected_channels = 1
        self.languages = ["zh"]

    async def transcribe(self, pcm, language="auto", hotwords=None) -> ASRResult:  # type: ignore[override]
        return ASRResult(text=self.name)

    async def transcribe_stream(self, chunks, language="auto"):  # type: ignore[override]
        yield ASRPartial(text=self.name, is_final=True)


def _registry_with(monkeypatch, names, default=""):
    settings = Settings(asr_engine="stub", tts_engine="stub", default_asr_model=default)
    reg = EngineRegistry(settings)
    # 替换内部 ASR 字典为可控的多引擎集合, 验证选择逻辑
    reg._asr = {n: _FakeASR(n) for n in names}  # type: ignore[attr-defined]
    reg._default_asr = reg._resolve_default_asr()  # type: ignore[attr-defined]
    return reg


def test_default_asr_first_when_unset(monkeypatch):
    reg = _registry_with(monkeypatch, ["a", "b"], default="")
    assert reg.default_asr == "a"
    assert reg.asr().name == "a"


def test_default_asr_honored(monkeypatch):
    reg = _registry_with(monkeypatch, ["a", "b"], default="b")
    assert reg.default_asr == "b"
    assert reg.asr().name == "b"


def test_asr_select_by_name(monkeypatch):
    reg = _registry_with(monkeypatch, ["a", "b"], default="a")
    assert reg.asr("b").name == "b"


def test_asr_unknown_name_falls_back_to_default(monkeypatch):
    reg = _registry_with(monkeypatch, ["a", "b"], default="a")
    assert reg.asr("does-not-exist").name == "a"


def test_paraformer_registered_when_configured():
    s = Settings(
        asr_engine="funasr",
        tts_engine="stub",
        funasr_paraformer_model="iic/paraformer-zh",
    )
    reg = EngineRegistry(s)
    assert "funasr-sensevoice" in reg.asr_engines
    assert "funasr-paraformer-zh" in reg.asr_engines
    # 默认仍为第一个 (sensevoice)
    assert reg.default_asr == "funasr-sensevoice"


def test_paraformer_absent_when_not_configured():
    s = Settings(asr_engine="funasr", tts_engine="stub", funasr_paraformer_model="")
    reg = EngineRegistry(s)
    assert "funasr-paraformer-zh" not in reg.asr_engines
