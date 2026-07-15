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
    # 非语音事件过滤: SenseVoice 富文本标签里若命中咳嗽/喷嚏/呼吸/掌声/笑声/BGM 等
    # 非语言声, 判为噪声 (返回空文本), 交由上层门控丢弃, 避免咳嗽被误转成拟声词触发对话。
    funasr_drop_nonspeech_events: bool = True

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
    # SeacoParaformer 热词模型: 唯一真正支持热词偏置 (bias encoder) 的 ASR。
    # 留空则不注册。复用 paraformer flavor (同样不含标点, 挂 funasr_punc_model 恢复)。
    # 仅文件式路径生效 (offline 模型), 实时流式仍走 paraformer-online 无热词。
    funasr_seaco_model: str = ""
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
    cosyvoice_default_voice: str = "中文女"

    # ---- 分句流式 TTS (Phase 3 §7 B 线 TTFB 优化) ----
    # 开启后: 长文本按标点分句, 在同一 TTS 信号量内逐句流式合成,
    # 让第一短句快速出首块以降 TTFB。默认关 = 行为与基线一致。
    tts_sentence_stream: bool = False
    # 单句最大字符数: 超出则按次标点/硬截断进一步切短。
    tts_max_sentence_chars: int = 60
    # 单段最小字符数: >0 时贪心合并过短段, 让每段音频足够长以盖住
    # 下一段 LLM prefill, 消除句间播放间隙 (用一点 TTFB 换平滑)。0=不合并。
    tts_min_sentence_chars: int = 0

    # ---- 语音聊天 (Phase 3 能力扩展: speech-to-speech) ----
    # 总开关; False 或未配 agent_endpoint 时 /ws/chat 直接回 unavailable, 不影响 ASR/TTS。
    chat_enabled: bool = False
    # 远端 AI Agent: OpenAI 兼容 base (带或不带 /v1 均可, AgentClient 自动归一)。
    agent_endpoint: str = ""
    # 鉴权 key: 走 node-local .env.deploy, 不入库。
    agent_api_key: str = ""
    # 远端模型名, 如 gpt-4o / qwen-plus / ep-xxx。
    agent_model: str = ""
    # 系统人设: 语音场景要求简短口语化, 避免长篇回复拖慢 TTS。
    agent_system_prompt: str = "你是一个友好的语音助手，用简短、口语化的中文回答，每次回复尽量不超过三句话。"
    # 多轮窗口: 保留最近 N 轮 (user+assistant 成对); 超出截断, 防远端超 context。
    agent_max_turns: int = 8
    agent_temperature: float = 0.7
    agent_max_tokens: int = 1024
    # 思考模式开关 (Qwen3 系列等混合推理模型): 语音对话追求低延迟, 默认关闭思考
    # (enable_thinking=false), 避免首 token 前的长链思考拖慢响应 (实测 qwen3.7-plus
    # 思考开启首 token ~10s, 关闭后 ~0.6s)。仅对支持该参数的模型生效, 其余模型兼容忽略。
    agent_enable_thinking: bool = False
    # 前端可选模型白名单: /ws/chat 的 start 帧 model 字段只接受该列表内的值 (防注入
    # 未授权/不存在的模型名导致 404)。空列表 = 不允许前端切换, 只用 agent_model。
    agent_model_allowlist: list[str] = ["qwen-plus", "qwen3.7-plus"]
    # 出网超时 (秒): 首 token 超时单独设, 防远端挂死占连接。
    agent_timeout: float = 60.0
    agent_connect_timeout: float = 10.0
    agent_first_token_timeout: float = 15.0
    # 出网并发上限 (非 GPU): 限制同时挂在远端 Agent 的请求数。
    agent_concurrency: int = 4
    # 聊天回复音色 / 语速 (复用 CosyVoice 音色)。
    agent_tts_voice: str = "中文女"
    agent_tts_speed: float = 1.0
    # 聊天用 ASR 模型 name (留空=默认 ASR, 线上即 funasr-streaming 真流式)。
    agent_asr_model: str = ""
    # 聊天 TTS 是否按句流式 (独立于全局 tts_sentence_stream; 聊天默认开以降首声延迟)。
    chat_tts_sentence_stream: bool = True
    # barge-in (说话打断): 开启后 RESPONDING 期间继续收音跑 ASR, 识别到用户插话即
    # 中止当前 Agent 流 + TTS 播放, 回到 LISTENING。默认关 = 保持半双工 (无 AEC 最稳)。
    # 依赖前端浏览器 AEC 抑制外放回声; 用「识别到 >=N 个实际字」而非纯能量做判据, 滤回声。
    chat_barge_in: bool = False
    # 打断判据: RESPONDING 期间 ASR partial 累计实际字符数 >= 该值才触发打断 (滤残余回声/噪声)。
    chat_barge_in_min_chars: int = 2
    # 有效发言门控 (滤环境噪声误触发): LISTENING 拿到 VAD 定稿后, 仅当满足下列两项才
    # 交给 Agent, 否则丢弃继续听。挡掉噪声被误识别成的单字/瞬时脉冲。
    # 定稿文本去标点/空白后的实际字数下限 (<该值视为噪声, 如误识别的单字)。
    chat_min_speech_chars: int = 2
    # 该段语音累计时长下限 (毫秒; <该值视为瞬时噪声脉冲)。
    chat_min_speech_ms: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
