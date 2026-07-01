from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import numpy as np


@dataclass
class ASRResult:
    text: str
    segments: list[dict] = field(default_factory=list)


@dataclass
class ASRPartial:
    text: str
    is_final: bool = False
    segment_id: int = 0


class ASREngine(ABC):
    """ASR 推理引擎契约。

    expected_sample_rate / expected_channels 是模型对输入的硬性要求,
    供 L3 预处理层做参数协商与自动适配。
    """

    name: str = "base-asr"
    expected_sample_rate: int = 16000
    expected_channels: int = 1
    languages: list[str] = ["zh", "en"]
    # 是否真正支持热词偏置 (bias encoder)。默认 False; 仅 SeacoParaformer 及
    # 以其定稿的流式引擎为 True。供前端按能力启用/禁用热词输入框。
    supports_hotwords: bool = False

    @abstractmethod
    async def transcribe(
        self,
        pcm: np.ndarray,
        language: str = "auto",
        hotwords: Optional[list[str]] = None,
    ) -> ASRResult:
        """整段转写 (文件式)。pcm: float32 单声道, 采样率 = expected_sample_rate。"""

    @abstractmethod
    async def transcribe_stream(
        self,
        chunks: AsyncIterator[np.ndarray],
        language: str = "auto",
        hotwords: Optional[list[str]] = None,
    ) -> AsyncIterator[ASRPartial]:
        """流式转写。输入 PCM 块异步迭代器, 产出 partial/final。

        hotwords 仅对支持 2pass 定稿的实现 (FunASRStreamingEngine) 生效:
        句末定稿时改用热词引擎重解码。第一遍滚动临时字始终无热词。
        """

    async def warmup(self) -> None:
        """预热: 触发权重加载 / kernel 编译。Phase 2 强化。"""

    def is_ready(self) -> bool:
        return True


class TTSEngine(ABC):
    """TTS 推理引擎契约。output_sample_rate 为模型输出 PCM 采样率。"""

    name: str = "base-tts"
    output_sample_rate: int = 24000
    voices: list[str] = ["中文女"]

    @abstractmethod
    async def synthesize(self, text: str, voice: str = "中文女", speed: float = 1.0) -> np.ndarray:
        """整段合成, 返回 float32 单声道 PCM (采样率 = output_sample_rate)。"""

    @abstractmethod
    async def synthesize_stream(
        self, text: str, voice: str = "中文女", speed: float = 1.0
    ) -> AsyncIterator[np.ndarray]:
        """流式合成, 逐块产出 PCM。"""

    async def warmup(self) -> None:
        ...

    def is_ready(self) -> bool:
        return True
