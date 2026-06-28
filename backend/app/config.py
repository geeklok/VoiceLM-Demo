from functools import lru_cache
from typing import Literal, Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceRef(BaseModel):
    """CosyVoice2 零样本参考音色: 参考音频 (16k wav) + 其对应文本。

    CosyVoice2-0.5B 原生只提供 zero-shot 克隆 (inference_zero_shot),
    内置 SFT 音色需额外下载 CosyVoice-300M-SFT 的 spk2info.pt。
    因此本服务用「预注册参考音频」的方式提供稳定可选音色。
    """

    audio_path: str
    prompt_text: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "asr-tts-backend"
    cors_origins: list[str] = ["*"]

    # 节点标识 (Phase 3): 每台机器经 env 注入 (node1 / node2), 随请求回显给前端,
    # 用于灰度对比 —— B 线推理优化只在入口机, 扩展机保持旧版, 前端/看板按 node 区分。
    node_name: str = "local"

    # 引擎选择: stub(本地无GPU) | funasr | cosyvoice
    asr_engine: Literal["stub", "funasr"] = "stub"
    tts_engine: Literal["stub", "cosyvoice"] = "stub"

    # 计算设备: cpu | mps(Apple Silicon) | cuda
    device: Literal["cpu", "mps", "cuda"] = "cpu"

    # 音频预处理目标参数 (模型契约)
    target_sample_rate: int = 16000
    target_channels: int = 1
    max_upload_bytes: int = 50 * 1024 * 1024

    # ---- FunASR (ASR) ----
    # ModelScope id 或本地权重目录
    funasr_model: str = "iic/SenseVoiceSmall"
    funasr_vad_model: str = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
    # VAD 单段最大时长 (ms), 长音频切分用; SenseVoice 推荐 30000
    funasr_vad_max_segment_ms: int = 30000
    funasr_use_itn: bool = True

    # ---- 真流式 ASR (Phase 3: 实时录音转写 2pass) ----
    # paraformer 流式模型 id/路径; 留空 = 不注册流式引擎 (节点保持伪流式基线)。
    funasr_streaming_model: str = ""
    # 聚合窗 (ms): 把前端 ~85ms 小块聚合到该时长再喂模型, 平衡延迟与精度。
    funasr_streaming_chunk_ms: int = 600
    # paraformer 流式 chunk_size [回看, 当前, 前看], 600ms 对应 [0,10,5]。
    funasr_streaming_chunk_size: list[int] = [0, 10, 5]
    funasr_streaming_encoder_look_back: int = 4
    funasr_streaming_decoder_look_back: int = 1
    # 2pass 修正: 句末用 offline SenseVoice 整句重解码覆盖流式临时字。
    funasr_stream_correct_enabled: bool = True

    # ---- 多模型 (Phase 2 §6.1) ----
    # 第二个 ASR: Paraformer-zh (中文高精度 + 时间戳 + 标点)。留空则不注册。
    funasr_paraformer_model: str = ""
    # Paraformer 标点模型 (留空则用其自带的 vad-punc 组合模型)。
    funasr_punc_model: str = ""
    # 默认 ASR 模型对外 name; 留空 = 第一个注册的引擎。
    default_asr_model: str = ""

    # ---- 并发控制 (Phase 2 §6.3, 单卡防 OOM/雪崩) ----
    # 同时占用 GPU 的 ASR / TTS 请求上限; 单 T4 建议 1-2。
    asr_concurrency: int = 2
    tts_concurrency: int = 2
    # 排队等待许可的最长时间 (秒); 超时返回 429 + Retry-After。
    gpu_acquire_timeout: float = 10.0
    gpu_retry_after: int = 2

    # ---- 降级链 + 熔断 (Phase 2 §6.4) ----
    # 文件式 ASR 主引擎失败时, 自动降级到其余已注册引擎。
    asr_fallback_enabled: bool = True
    # 单次 ASR 推理超时 (秒); 0 = 不设超时 (长音频安全)。超时视为故障并降级。
    asr_infer_timeout: float = 0.0
    # 连续失败达到阈值即熔断该引擎, cooldown 秒内短路跳过。
    breaker_fail_threshold: int = 3
    breaker_cooldown: float = 30.0

    # ---- 优雅退出 (Phase 2 §6.5) ----
    # 收到 SIGTERM 后, 等待存量请求清空的最长时间 (秒)。
    drain_timeout: float = 25.0

    # ---- CosyVoice2 (TTS) ----
    # CosyVoice 仓库根目录: 用于把 third_party/Matcha-TTS 注入 sys.path
    cosyvoice_repo_dir: Optional[str] = None
    # 模型权重目录 (本地路径) 或 ModelScope id
    cosyvoice_model: str = "iic/CosyVoice2-0.5B"
    cosyvoice_load_jit: bool = False
    cosyvoice_load_trt: bool = False
    cosyvoice_load_vllm: bool = False
    cosyvoice_fp16: bool = False
    # 零样本参考音色: 音色名 -> 参考音频 + 文本
    cosyvoice_voices: dict[str, VoiceRef] = {}
    # 默认音色名 (须存在于 cosyvoice_voices)
    cosyvoice_default_voice: str = "default"

    # ---- 分句流式 TTS (Phase 3 §7 B 线 TTFB 优化) ----
    # 开启后: 长文本按标点分句, 在同一 TTS 信号量内逐句流式合成,
    # 让第一短句快速出首块以降 TTFB。默认关 = 行为与基线一致。
    tts_sentence_stream: bool = False
    # 单句最大字符数: 超出则按次标点/硬截断进一步切短。
    tts_max_sentence_chars: int = 60
    # 单段最小字符数: >0 时贪心合并过短段, 让每段音频足够长以盖住
    # 下一段 LLM prefill, 消除句间播放间隙 (用一点 TTFB 换平滑)。0=不合并。
    tts_min_sentence_chars: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()
