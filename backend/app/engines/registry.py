from __future__ import annotations

from app.config import Settings
from app.engines.base import ASREngine, TTSEngine
from app.utils.logging import get_logger

logger = get_logger(__name__)


class EngineRegistry:
    """引擎注册表。MVP 持有单个 ASR + 单个 TTS 引擎,
    Phase 2 扩展为多引擎按 name 选择。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._asr = self._build_asr()
        self._tts = self._build_tts()

    def _build_asr(self) -> ASREngine:
        if self._settings.asr_engine == "funasr":
            from app.engines.funasr_engine import FunASREngine

            return FunASREngine(self._settings)
        from app.engines.stub_engine import StubASREngine

        return StubASREngine()

    def _build_tts(self) -> TTSEngine:
        if self._settings.tts_engine == "cosyvoice":
            from app.engines.cosyvoice_engine import CosyVoiceEngine

            return CosyVoiceEngine(self._settings)
        from app.engines.stub_engine import StubTTSEngine

        return StubTTSEngine()

    @property
    def asr(self) -> ASREngine:
        return self._asr

    @property
    def tts(self) -> TTSEngine:
        return self._tts

    async def warmup(self) -> None:
        logger.info("warming up engines asr=%s tts=%s", self._asr.name, self._tts.name)
        await self._asr.warmup()
        await self._tts.warmup()

    def is_ready(self) -> bool:
        return self._asr.is_ready() and self._tts.is_ready()
