from __future__ import annotations

import asyncio

import numpy as np

from app.config import Settings
from app.engines.funasr_engine import FunASREngine


class _FakeSenseVoice:
    """假 SenseVoice: 按预设返回带富文本事件标签的原始 item (含 <|event|>)。"""

    def __init__(self, raw_texts: list[str]) -> None:
        self._raw = raw_texts

    def generate(self, **kwargs):
        return [{"text": t, "key": f"k{i}"} for i, t in enumerate(self._raw)]


def _make_engine(raw_texts: list[str], *, drop: bool = True) -> FunASREngine:
    settings = Settings(
        asr_engine="funasr", tts_engine="stub",
        funasr_drop_nonspeech_events=drop,
    )
    eng = FunASREngine(settings, flavor="sensevoice")
    eng._model = _FakeSenseVoice(raw_texts)  # type: ignore[attr-defined]
    eng._load = lambda: None  # type: ignore[assignment]
    # 跳过真实 funasr 后处理依赖: 剥掉标签, 保留正文 (取 '>' 之后)。
    eng._postprocess = lambda t: t.split(">")[-1].strip()  # type: ignore[assignment]
    return eng


def _run(eng: FunASREngine) -> str:
    pcm = np.zeros(16000, dtype=np.float32)
    return asyncio.run(eng.transcribe(pcm)).text


def test_cough_event_dropped():
    # 咳嗽被标 <|Cough|> + 硬转拟声词 "嗯哼"; 应整段丢弃 -> 空文本。
    eng = _make_engine(["<|zh|><|EMO_UNKNOWN|><|Cough|><|withitn|>嗯哼。"])
    assert _run(eng) == ""


def test_speech_event_passes():
    # 正常说话标 <|Speech|>; 不受影响, 正文透出。
    eng = _make_engine(["<|zh|><|NEUTRAL|><|Speech|><|withitn|>今天天气不错。"])
    assert _run(eng) == "今天天气不错。"


def test_other_nonspeech_events_dropped():
    for raw in (
        "<|zh|><|EMO_UNKNOWN|><|BGM|><|withitn|>啦啦。",
        "<|en|><|NEUTRAL|><|Applause|><|withitn|>clap.",
        "<|zh|><|EMO_UNKNOWN|><|Breath|><|withitn|>呼。",
    ):
        assert _run(_make_engine([raw])) == ""


def test_speech_with_cough_kept():
    # 说话中夹杂咳嗽标签 (共现 <|Speech|>): 保守放行, 不丢。
    eng = _make_engine(["<|zh|><|NEUTRAL|><|Speech|><|Cough|><|withitn|>你好。"])
    assert _run(eng) == "你好。"


def test_disabled_keeps_cough():
    # 开关关闭: 咳嗽也透出 (回归旧行为)。
    eng = _make_engine(
        ["<|zh|><|EMO_UNKNOWN|><|Cough|><|withitn|>嗯哼。"], drop=False
    )
    assert _run(eng) == "嗯哼。"


def test_mixed_batch_drops_only_nonspeech():
    # 一批多段: 咳嗽段丢弃, 语音段保留并拼接。
    eng = _make_engine([
        "<|zh|><|EMO_UNKNOWN|><|Cough|><|withitn|>咳。",
        "<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好啊。",
    ])
    assert _run(eng) == "你好啊。"
