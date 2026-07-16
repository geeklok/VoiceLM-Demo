from typing import Literal, Optional

from pydantic import BaseModel, Field


class ASRSegment(BaseModel):
    text: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


class ASRResponse(BaseModel):
    text: str
    segments: list[ASRSegment] = Field(default_factory=list)
    audio_duration_ms: int = 0
    process_ms: int = 0
    rtf: float = 0.0
    model: str = ""
    # 实际使用的引擎是否由请求/默认模型降级而来 (Phase 2 §6.4)
    degraded: bool = False
    # 处理该请求的节点 (Phase 3 灰度对比); 由后端 NODE_NAME 注入
    node: str = ""


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str = "中文女"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    format: str = Field(default="wav", pattern="^(wav)$")
    model: Optional[str] = None
    # 领域 TN 预处理类别 (前端多选); 送模型内置 TN 前按类做归一化。空/None=不做。
    domain_tn: Optional[list[str]] = None


class ModelInfo(BaseModel):
    name: str
    kind: str  # "asr" | "tts"
    expected_sample_rate: int
    languages: list[str] = Field(default_factory=list)
    default: bool = False
    # 是否支持热词偏置 (仅 ASR); 前端据此启用/禁用热词输入框。
    supports_hotwords: bool = False


class ModelsResponse(BaseModel):
    asr: list[ModelInfo] = Field(default_factory=list)
    tts: list[ModelInfo] = Field(default_factory=list)


class ChatModelInfo(BaseModel):
    name: str
    label: str
    mode: Literal["cascade", "native"]
    provider: str
    voices: list[str] = Field(default_factory=list)
    default: bool = False
    input_format: Literal["pcm_f32le", "pcm_s16le"] = "pcm_f32le"
    supports_thinking: bool = False
    supports_barge_in: bool = True
    supports_vad_gate: bool = True
    preserves_paralinguistics: bool = False


class ChatModelsResponse(BaseModel):
    models: list[ChatModelInfo] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    ready: bool


class TnCategory(BaseModel):
    """领域 TN 类别 (单条)。前端据此动态渲染多选框, 避免前后端手写双维护。"""

    id: str
    label: str
    # 是否已真正实现 (规则可做对); False = 实验性·暂未生效 (noop 透传)。
    impl: bool
