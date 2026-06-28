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
