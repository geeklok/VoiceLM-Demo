from __future__ import annotations

from typing import Optional

from app.config import Settings
from app.engines.base import ASREngine, TTSEngine
from app.utils.logging import get_logger

logger = get_logger(__name__)


class EngineRegistry:
    """引擎注册表 (Phase 2 §6.1)。持有多个 ASR / TTS 引擎, 按 name 选择。

    默认引擎: ASR 取 settings.default_asr_model (留空则第一个注册的);
    TTS 当前仍单引擎 (CosyVoice2 多音色已覆盖音色维度的选择)。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._asr: dict[str, ASREngine] = {}
        self._tts: dict[str, TTSEngine] = {}
        self._build_asr()
        self._build_tts()
        self._default_asr = self._resolve_default_asr()
        self._default_tts = next(iter(self._tts), "")

    def _build_asr(self) -> None:
        s = self._settings
        if s.asr_engine == "funasr":
            from app.engines.funasr_engine import FunASREngine

            sense = FunASREngine(
                s, name="funasr-sensevoice", flavor="sensevoice", model=s.funasr_model
            )
            self._asr[sense.name] = sense
            # 第二个 ASR: Paraformer-zh (可选, 配置了 model id 才注册)。
            if s.funasr_paraformer_model:
                para = FunASREngine(
                    s,
                    name="funasr-paraformer-zh",
                    flavor="paraformer",
                    model=s.funasr_paraformer_model,
                    punc_model=s.funasr_punc_model,
                )
                self._asr[para.name] = para
            # 真流式 2pass 引擎 (Phase 3, 可选): 配了流式 model id 才注册,
            # 复用 sensevoice 作 offline 修正引擎 (零额外大模型显存)。
            if s.funasr_streaming_model:
                from app.engines.funasr_engine import FunASRStreamingEngine

                streaming = FunASRStreamingEngine(
                    s, name="funasr-streaming", offline_engine=sense
                )
                self._asr[streaming.name] = streaming
        else:
            from app.engines.stub_engine import StubASREngine

            stub = StubASREngine()
            self._asr[stub.name] = stub

    def _build_tts(self) -> None:
        if self._settings.tts_engine == "cosyvoice":
            from app.engines.cosyvoice_engine import CosyVoiceEngine

            eng: TTSEngine = CosyVoiceEngine(self._settings)
        else:
            from app.engines.stub_engine import StubTTSEngine

            eng = StubTTSEngine()
        self._tts[eng.name] = eng

    def _resolve_default_asr(self) -> str:
        want = self._settings.default_asr_model
        if want and want in self._asr:
            return want
        return next(iter(self._asr), "")

    def asr(self, name: Optional[str] = None) -> ASREngine:
        if name and name in self._asr:
            return self._asr[name]
        return self._asr[self._default_asr]

    def asr_chain(self, name: Optional[str] = None) -> list[ASREngine]:
        """降级链 (Phase 2 §6.4): 首选引擎在前, 其余已注册引擎依次兜底。

        首选 = 指定 name (未知/None 则默认引擎)。链表去重, 保持注册顺序。
        """
        primary = self.asr(name)
        chain = [primary]
        for eng in self._asr.values():
            if eng.name != primary.name:
                chain.append(eng)
        return chain

    def tts(self, name: Optional[str] = None) -> TTSEngine:
        if name and name in self._tts:
            return self._tts[name]
        return self._tts[self._default_tts]

    @property
    def asr_engines(self) -> dict[str, ASREngine]:
        return self._asr

    @property
    def tts_engines(self) -> dict[str, TTSEngine]:
        return self._tts

    @property
    def default_asr(self) -> str:
        return self._default_asr

    @property
    def default_tts(self) -> str:
        return self._default_tts

    async def warmup(self) -> None:
        logger.info(
            "warming up engines asr=%s tts=%s",
            list(self._asr.keys()),
            list(self._tts.keys()),
        )
        for eng in self._asr.values():
            await eng.warmup()
        for eng in self._tts.values():
            await eng.warmup()

    def is_ready(self) -> bool:
        return all(e.is_ready() for e in self._asr.values()) and all(
            e.is_ready() for e in self._tts.values()
        )
