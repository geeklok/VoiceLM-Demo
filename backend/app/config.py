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


@lru_cache
def get_settings() -> Settings:
    return Settings()
