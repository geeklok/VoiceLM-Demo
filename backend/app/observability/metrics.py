from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Phase 3 §7.1/7.3: QoS 指标体系。独立 registry, 避免与第三方库默认 registry 冲突;
# /metrics 端点用本 registry 渲染。所有埋点经本模块的 helper 上报, 业务代码不直接碰指标对象。

REGISTRY = CollectorRegistry()

# ---- ASR ----
ASR_REQUESTS = Counter(
    "asr_requests_total",
    "ASR 请求总数",
    ["model", "status", "degraded"],
    registry=REGISTRY,
)
ASR_RTF = Histogram(
    "asr_rtf",
    "ASR 实时率 (处理时长 / 音频时长), 越小越好",
    ["model"],
    buckets=(0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0),
    registry=REGISTRY,
)
ASR_PROCESS_SECONDS = Histogram(
    "asr_process_seconds",
    "ASR 单请求推理耗时 (秒)",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)
ASR_AUDIO_SECONDS = Histogram(
    "asr_audio_seconds",
    "ASR 输入音频时长 (秒)",
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)
ASR_STREAM_SESSIONS = Counter(
    "asr_stream_sessions_total",
    "实时 ASR 会话数",
    ["model", "status"],
    registry=REGISTRY,
)
ASR_STREAM_FIRST_RESULT_SECONDS = Histogram(
    "asr_stream_first_result_seconds",
    "实时 ASR 从首块输入音频到首个转写结果的耗时 (秒)",
    ["model"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0),
    registry=REGISTRY,
)
ASR_STREAM_REVISIONS = Counter(
    "asr_stream_revisions_total",
    "实时 ASR 同一分段文本发生修订的次数",
    ["model"],
    registry=REGISTRY,
)
ASR_STREAM_FINALS = Counter(
    "asr_stream_finals_total",
    "实时 ASR 定稿分段数",
    ["model"],
    registry=REGISTRY,
)

# ---- TTS ----
TTS_REQUESTS = Counter(
    "tts_requests_total",
    "TTS 请求总数",
    ["model", "mode", "status"],  # mode: file | stream
    registry=REGISTRY,
)
TTS_TTFB_SECONDS = Histogram(
    "tts_ttfb_seconds",
    "TTS 首包延迟 (流式首个音频块返回耗时, 秒), CosyVoice2 目标 ~0.15s",
    ["model"],
    # 低区细分对齐 ~0.15s 目标 (衡量 B 线优化效果); 高区放宽到 10s,
    # 因优化前 baseline 首包随文本长度可达数秒, 5s 封顶会让分位数饱和失真。
    buckets=(0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0),
    registry=REGISTRY,
)
TTS_RTF = Histogram(
    "tts_rtf",
    "TTS 实时率 (合成耗时 / 音频时长), <1 才能实时",
    ["model"],
    buckets=(0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 5.0),
    registry=REGISTRY,
)
TTS_PROCESS_SECONDS = Histogram(
    "tts_process_seconds",
    "TTS 单请求合成总耗时 (秒)",
    ["model", "mode"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

# ---- 语音聊天 ----
CHAT_SESSIONS = Counter(
    "chat_sessions_total",
    "语音聊天会话数",
    ["provider", "model", "status"],
    registry=REGISTRY,
)
CHAT_TURNS = Counter(
    "chat_turns_total",
    "语音聊天回合数",
    ["provider", "model", "status"],
    registry=REGISTRY,
)
CHAT_ENDPOINT_SECONDS = Histogram(
    "chat_endpoint_seconds",
    "最后一块用户音频送入后到用户语音定稿的耗时 (秒)",
    ["provider", "model"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0),
    registry=REGISTRY,
)
CHAT_FIRST_AUDIO_SECONDS = Histogram(
    "chat_first_audio_seconds",
    "用户语音定稿后到首块回复音频下发的耗时 (秒)",
    ["provider", "model"],
    buckets=(0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 10.0),
    registry=REGISTRY,
)
CHAT_TURN_SECONDS = Histogram(
    "chat_turn_seconds",
    "用户语音定稿后到回复生成完成的耗时 (秒)",
    ["provider", "model"],
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 20.0, 30.0, 60.0),
    registry=REGISTRY,
)
CHAT_INTERRUPT_SECONDS = Histogram(
    "chat_interrupt_seconds",
    "确认用户打断后到停止当前回复并发出打断事件的耗时 (秒)",
    ["provider", "model"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0),
    registry=REGISTRY,
)
CHAT_AUDIO_SECONDS = Counter(
    "chat_audio_seconds_total",
    "语音聊天处理的音频时长 (秒)",
    ["provider", "model", "direction"],
    registry=REGISTRY,
)
CHAT_TOKENS = Counter(
    "chat_tokens_total",
    "语音聊天 Provider 返回的 Token 用量，用于按模型价目表核算费用",
    ["provider", "model", "direction", "modality"],
    registry=REGISTRY,
)
CHAT_PROVIDER_ERRORS = Counter(
    "chat_provider_errors_total",
    "语音聊天 Provider 错误数",
    ["provider", "model", "code"],
    registry=REGISTRY,
)
CHAT_PLAYBACK_UNDERRUNS = Counter(
    "chat_playback_underruns_total",
    "前端报告的语音聊天播放欠载次数",
    ["provider", "model"],
    registry=REGISTRY,
)
CHAT_PLAYBACK_DROPPED_CHUNKS = Counter(
    "chat_playback_dropped_chunks_total",
    "前端有界播放缓冲为限制积压而丢弃的音频块数",
    ["provider", "model"],
    registry=REGISTRY,
)

# ---- GPU 闸门 / 容量 (Phase 2 §6.3 限流器可观测化) ----
GPU_INFLIGHT = Gauge(
    "gpu_inflight",
    "当前占用 GPU 信号量的请求数",
    ["kind"],  # asr | tts
    registry=REGISTRY,
)
GPU_QUEUE_REJECTIONS = Counter(
    "gpu_queue_rejections_total",
    "排队超时被拒 (429) 的请求数",
    ["kind"],
    registry=REGISTRY,
)
INFERENCE_FAILURES = Counter(
    "inference_failures_total",
    "推理引擎调用失败次数 (触发降级/熔断)",
    ["engine"],
    registry=REGISTRY,
)


def observe_asr(model: str, *, status: str, degraded: bool,
                process_ms: int, audio_ms: int, rtf: float) -> None:
    ASR_REQUESTS.labels(model=model, status=status, degraded=str(degraded).lower()).inc()
    if status == "ok":
        ASR_PROCESS_SECONDS.labels(model=model).observe(process_ms / 1000.0)
        ASR_RTF.labels(model=model).observe(rtf)
        if audio_ms:
            ASR_AUDIO_SECONDS.observe(audio_ms / 1000.0)


def observe_asr_stream(
    model: str,
    *,
    status: str,
    first_result_ms: int | None = None,
    revisions: int = 0,
    finals: int = 0,
) -> None:
    ASR_STREAM_SESSIONS.labels(model=model, status=status).inc()
    if first_result_ms is not None:
        ASR_STREAM_FIRST_RESULT_SECONDS.labels(model=model).observe(
            first_result_ms / 1000.0
        )
    if revisions > 0:
        ASR_STREAM_REVISIONS.labels(model=model).inc(revisions)
    if finals > 0:
        ASR_STREAM_FINALS.labels(model=model).inc(finals)


def observe_tts(model: str, mode: str, *, status: str,
                process_ms: int | None = None,
                ttfb_ms: int | None = None,
                rtf: float | None = None) -> None:
    TTS_REQUESTS.labels(model=model, mode=mode, status=status).inc()
    if status != "ok":
        return
    if process_ms is not None:
        TTS_PROCESS_SECONDS.labels(model=model, mode=mode).observe(process_ms / 1000.0)
    if ttfb_ms is not None:
        TTS_TTFB_SECONDS.labels(model=model).observe(ttfb_ms / 1000.0)
    if rtf is not None:
        TTS_RTF.labels(model=model).observe(rtf)


def observe_chat_session(provider: str, model: str, status: str) -> None:
    CHAT_SESSIONS.labels(provider=provider, model=model, status=status).inc()


def observe_chat_turn(
    provider: str,
    model: str,
    *,
    status: str,
    endpoint_ms: int | None = None,
    first_audio_ms: int | None = None,
    total_ms: int | None = None,
    input_audio_ms: int | None = None,
    output_audio_ms: int | None = None,
    interrupt_ms: int | None = None,
) -> None:
    CHAT_TURNS.labels(provider=provider, model=model, status=status).inc()
    labels = {"provider": provider, "model": model}
    if endpoint_ms is not None:
        CHAT_ENDPOINT_SECONDS.labels(**labels).observe(endpoint_ms / 1000.0)
    if first_audio_ms is not None:
        CHAT_FIRST_AUDIO_SECONDS.labels(**labels).observe(first_audio_ms / 1000.0)
    if total_ms is not None:
        CHAT_TURN_SECONDS.labels(**labels).observe(total_ms / 1000.0)
    if input_audio_ms is not None:
        CHAT_AUDIO_SECONDS.labels(**labels, direction="input").inc(input_audio_ms / 1000.0)
    if output_audio_ms is not None:
        CHAT_AUDIO_SECONDS.labels(**labels, direction="output").inc(output_audio_ms / 1000.0)
    if interrupt_ms is not None:
        CHAT_INTERRUPT_SECONDS.labels(**labels).observe(interrupt_ms / 1000.0)


def observe_chat_provider_error(
    provider: str, model: str, code: str
) -> None:
    CHAT_PROVIDER_ERRORS.labels(provider=provider, model=model, code=code).inc()


def observe_chat_usage(provider: str, model: str, usage: dict | None) -> None:
    if not usage:
        return
    for direction in ("input", "output"):
        details = usage.get(f"{direction}_tokens_details") or {}
        observed = False
        for modality in ("text", "audio"):
            value = details.get(f"{modality}_tokens")
            if isinstance(value, (int, float)) and value >= 0:
                CHAT_TOKENS.labels(
                    provider=provider,
                    model=model,
                    direction=direction,
                    modality=modality,
                ).inc(value)
                observed = True
        if not observed:
            value = usage.get(f"{direction}_tokens")
            if isinstance(value, (int, float)) and value >= 0:
                CHAT_TOKENS.labels(
                    provider=provider,
                    model=model,
                    direction=direction,
                    modality="all",
                ).inc(value)


def observe_chat_playback_underrun(
    provider: str, model: str, count: int = 1
) -> None:
    if count > 0:
        CHAT_PLAYBACK_UNDERRUNS.labels(provider=provider, model=model).inc(count)


def observe_chat_playback_dropped_chunks(
    provider: str, model: str, count: int = 1
) -> None:
    if count > 0:
        CHAT_PLAYBACK_DROPPED_CHUNKS.labels(
            provider=provider, model=model
        ).inc(count)
