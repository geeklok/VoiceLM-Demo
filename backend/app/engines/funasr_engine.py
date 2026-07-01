from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

import numpy as np

from app.config import Settings
from app.engines.base import ASREngine, ASRPartial, ASRResult
from app.utils.logging import get_logger

logger = get_logger(__name__)

# SenseVoiceSmall 支持的语言标签; 其余值回退到 auto
_SUPPORTED_LANGS = {"auto", "zh", "en", "yue", "ja", "ko", "nospeech"}


class FunASREngine(ASREngine):
    """FunASR 封装 (阿里 DAMO)。支持两种 flavor:

    - ``sensevoice``: SenseVoiceSmall, 多语种、低延迟、自带富文本后处理 (情感/事件标签)。
    - ``paraformer``: Paraformer-zh, 中文高精度、自带时间戳, 搭配标点模型恢复标点。

    funasr 推理为同步阻塞调用, 统一用 asyncio.to_thread 包裹避免阻塞事件循环。
    依赖 ``pip install -e .[engines]``; 本地无 GPU 时改用 stub 引擎。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        name: str = "funasr-sensevoice",
        flavor: str = "sensevoice",
        model: Optional[str] = None,
        punc_model: str = "",
        languages: Optional[list[str]] = None,
        supports_hotwords: bool = False,
    ) -> None:
        self._settings = settings
        self.name = name
        self._flavor = flavor
        self._model_id = model or settings.funasr_model
        self._punc_model = punc_model
        self.expected_sample_rate = 16000
        self.expected_channels = 1
        self.supports_hotwords = supports_hotwords
        self.languages = languages or (
            ["auto", "zh", "en", "yue", "ja", "ko"] if flavor == "sensevoice" else ["zh"]
        )
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from funasr import AutoModel  # lazy import

        s = self._settings
        kwargs: dict = {
            "model": self._model_id,
            "vad_model": s.funasr_vad_model,
            "vad_kwargs": {"max_single_segment_time": s.funasr_vad_max_segment_ms},
            "device": s.device,
            "disable_update": True,
        }
        # Paraformer 自身不含标点, 显式挂标点模型恢复标点 (SenseVoice 自带, 不需要)。
        if self._flavor == "paraformer" and self._punc_model:
            kwargs["punc_model"] = self._punc_model
        logger.info(
            "loading FunASR name=%s model=%s flavor=%s device=%s",
            self.name,
            self._model_id,
            self._flavor,
            s.device,
        )
        self._model = AutoModel(**kwargs)

    def is_ready(self) -> bool:
        return self._model is not None

    async def warmup(self) -> None:
        await asyncio.to_thread(self._load)
        silence = np.zeros(self.expected_sample_rate, dtype=np.float32)
        await self.transcribe(silence)

    async def transcribe(
        self, pcm: np.ndarray, language: str = "auto", hotwords: Optional[list[str]] = None
    ) -> ASRResult:
        def _run() -> ASRResult:
            self._load()

            kwargs = {
                "input": np.ascontiguousarray(pcm, dtype=np.float32),
                "fs": self.expected_sample_rate,
                "cache": {},
                "use_itn": self._settings.funasr_use_itn,
                "batch_size_s": 60,
            }
            # SenseVoice 支持 language 标签; Paraformer-zh 为纯中文模型, 不接受该参数。
            if self._flavor == "sensevoice":
                kwargs["language"] = language if language in _SUPPORTED_LANGS else "auto"
            if hotwords:
                kwargs["hotword"] = " ".join(hotwords)
            res = self._model.generate(**kwargs)

            segments: list[dict] = []
            texts: list[str] = []
            for item in res or []:
                clean = self._postprocess(item.get("text", ""))
                if not clean:
                    continue
                texts.append(clean)
                segments.append({"text": clean, "key": item.get("key", "")})
            return ASRResult(text="".join(texts), segments=segments)

        return await asyncio.to_thread(_run)

    def _postprocess(self, text: str) -> str:
        # SenseVoice 输出含 <|emo|><|event|> 等富文本标记, 需专用后处理剥离。
        if self._flavor == "sensevoice":
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            return rich_transcription_postprocess(text)
        return text.strip()

    async def transcribe_stream(
        self, chunks: AsyncIterator[np.ndarray], language: str = "auto",
        hotwords: Optional[list[str]] = None,
    ) -> AsyncIterator[ASRPartial]:
        # MVP: 累积音频后整段转写 (SenseVoice 为非自回归, 走 offline pass)。
        # 真正的低延迟流式 partial 在 Phase 3 引入 paraformer-streaming / 2pass。
        buf: list[np.ndarray] = []
        async for chunk in chunks:
            buf.append(chunk)
            secs = sum(len(c) for c in buf) / self.expected_sample_rate
            yield ASRPartial(text=f"... ({secs:.1f}s)", is_final=False)
        pcm = np.concatenate(buf) if buf else np.zeros(0, dtype=np.float32)
        result = await self.transcribe(pcm, language=language, hotwords=hotwords)
        yield ASRPartial(text=result.text, is_final=True)


class FunASRStreamingEngine(ASREngine):
    """真流式 2pass ASR (Phase 3: 实时录音转写)。

    第一遍 (低延迟出字): paraformer-zh-streaming 真流式模型, 按聚合窗逐块增量出字,
    每个聚合块产出当前句的临时文本 (is_final=False)。
    第二遍 (字符修正): 流式 fsmn-vad 检测句子端点, 句末把该句原始音频交给已加载的
    offline 引擎整句重解码, 产出修正后的定稿 (is_final=True) 覆盖临时字。

    定稿引擎动态选择 (零额外显存, 两引擎均已常驻):
    - 不带热词: 用 offline_engine (SenseVoice), 保多语种 + 情感富文本。
    - 带热词:   用 hotword_engine (SeacoParaformer), 唯一支持热词偏置的引擎。
      未注入 hotword_engine 时 (未配 seaco) 回退到 offline_engine, 热词被忽略。
    第一遍滚动临时字始终无热词 (paraformer-online 架构限制), 句末被定稿覆盖。

    复用已注册引擎做 2pass, 零额外大模型显存;
    仅额外加载 paraformer 流式 (~1GB) + 流式 vad 实例 (权重复用)。

    约定: ASRPartial.text 承载「当前这一句」的文本 (非累计全文), segment_id 为句序号。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        name: str = "funasr-streaming",
        offline_engine: ASREngine,
        hotword_engine: Optional[ASREngine] = None,
    ) -> None:
        self._settings = settings
        self.name = name
        self._offline = offline_engine
        self._hotword = hotword_engine
        self.expected_sample_rate = 16000
        self.expected_channels = 1
        # 仅当注入了热词定稿引擎 (seaco) 时才对外声明支持热词; 否则句末定稿走
        # SenseVoice, 热词无效 -> 前端应禁用热词框。
        self.supports_hotwords = hotword_engine is not None
        self.languages = ["auto", "zh", "en"]
        self._asr = None
        self._vad = None

    def _load(self) -> None:
        if self._asr is not None:
            return
        from funasr import AutoModel  # lazy import

        s = self._settings
        logger.info(
            "loading FunASR streaming name=%s model=%s vad=%s device=%s",
            self.name,
            s.funasr_streaming_model,
            s.funasr_vad_model,
            s.device,
        )
        self._asr = AutoModel(
            model=s.funasr_streaming_model, device=s.device, disable_update=True
        )
        self._vad = AutoModel(
            model=s.funasr_vad_model, device=s.device, disable_update=True
        )

    def is_ready(self) -> bool:
        return self._asr is not None

    async def warmup(self) -> None:
        await asyncio.to_thread(self._load)
        # 用 1s 静音跑一遍流式 generate, 触发权重加载 / kernel 编译。
        s = self._settings
        silence = np.zeros(self.expected_sample_rate, dtype=np.float32)
        await asyncio.to_thread(
            self._asr.generate,
            input=silence,
            cache={},
            is_final=True,
            chunk_size=s.funasr_streaming_chunk_size,
            encoder_chunk_look_back=s.funasr_streaming_encoder_look_back,
            decoder_chunk_look_back=s.funasr_streaming_decoder_look_back,
        )

    def _finalize_engine(self, hotwords: Optional[list[str]]) -> ASREngine:
        """定稿引擎: 带热词且已注入 hotword_engine 时用它, 否则用 offline。"""
        if hotwords and self._hotword is not None:
            return self._hotword
        return self._offline

    async def transcribe(
        self, pcm: np.ndarray, language: str = "auto", hotwords: Optional[list[str]] = None
    ) -> ASRResult:
        # 文件式: 委托定稿引擎 (带热词 -> seaco, 否则 SenseVoice), 质量与基线一致。
        engine = self._finalize_engine(hotwords)
        return await engine.transcribe(pcm, language=language, hotwords=hotwords)

    @staticmethod
    def _result_text(res) -> str:
        text = ""
        for item in res or []:
            text += item.get("text", "")
        return text

    @staticmethod
    def _vad_ended(res) -> bool:
        # fsmn-vad 流式: res[0]["value"] 为 [[beg, end], ...]; end != -1 表示句子结束。
        for item in res or []:
            for seg in item.get("value", []) or []:
                if isinstance(seg, (list, tuple)) and len(seg) == 2 and seg[1] != -1:
                    return True
        return False

    async def transcribe_stream(
        self, chunks: AsyncIterator[np.ndarray], language: str = "auto",
        hotwords: Optional[list[str]] = None,
    ) -> AsyncIterator[ASRPartial]:
        self._load()
        s = self._settings
        win = int(self.expected_sample_rate * s.funasr_streaming_chunk_ms / 1000)
        # 句末定稿引擎: 带热词 -> seaco, 否则 SenseVoice (整轮固定, 不逐句切)。
        finalize = self._finalize_engine(hotwords)
        buf: list[np.ndarray] = []
        seg_audio: list[np.ndarray] = []
        seg_text = ""
        seg_id = 0
        para_cache: dict = {}
        vad_cache: dict = {}

        def _flush(pcm_block: np.ndarray, is_final: bool) -> tuple[str, bool]:
            nonlocal seg_text
            block = np.ascontiguousarray(pcm_block, dtype=np.float32)
            piece = self._asr.generate(
                input=block,
                cache=para_cache,
                is_final=is_final,
                chunk_size=s.funasr_streaming_chunk_size,
                encoder_chunk_look_back=s.funasr_streaming_encoder_look_back,
                decoder_chunk_look_back=s.funasr_streaming_decoder_look_back,
            )
            seg_text += self._result_text(piece)
            vad_res = self._vad.generate(
                input=block,
                cache=vad_cache,
                is_final=is_final,
                chunk_size=s.funasr_streaming_chunk_ms,
            )
            return seg_text, self._vad_ended(vad_res)

        async def _correct(audio: list[np.ndarray], fallback: str) -> str:
            if not (s.funasr_stream_correct_enabled and audio):
                return fallback
            try:
                pcm = np.concatenate(audio)
                fixed = (
                    await finalize.transcribe(pcm, language=language, hotwords=hotwords)
                ).text
                return fixed or fallback
            except Exception:  # noqa: BLE001
                logger.warning("stream 2pass correction failed; keep streaming text")
                return fallback

        async for chunk in chunks:
            buf.append(chunk)
            seg_audio.append(chunk)
            if sum(len(c) for c in buf) < win:
                continue
            block = np.concatenate(buf)
            buf = []
            text, ended = await asyncio.to_thread(_flush, block, False)
            yield ASRPartial(text=text, is_final=False, segment_id=seg_id)
            if ended:
                final_text = await _correct(seg_audio, text)
                yield ASRPartial(text=final_text, is_final=True, segment_id=seg_id)
                seg_id += 1
                seg_text = ""
                seg_audio = []
                para_cache = {}
                vad_cache = {}

        # 收尾: flush 残余尾块 + 末句修正。
        if buf or seg_audio:
            tail = np.concatenate(buf) if buf else np.zeros(0, dtype=np.float32)
            text, _ = await asyncio.to_thread(_flush, tail, True)
            final_text = await _correct(seg_audio, text)
            if final_text:
                yield ASRPartial(text=final_text, is_final=True, segment_id=seg_id)
