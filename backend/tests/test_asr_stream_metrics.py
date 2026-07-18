from app.observability.metrics import (
    ASR_STREAM_FINALS,
    ASR_STREAM_FIRST_RESULT_SECONDS,
    ASR_STREAM_REVISIONS,
    ASR_STREAM_SESSIONS,
    observe_asr_stream,
)


def _value(collector, name: str, labels: dict[str, str]) -> float:
    for metric in collector.collect():
        for sample in metric.samples:
            if sample.name == name and sample.labels == labels:
                return float(sample.value)
    return 0.0


def test_asr_stream_metrics_record_latency_revisions_and_status() -> None:
    model = "test-stream-model"
    observe_asr_stream(
        model,
        status="disconnected",
        first_result_ms=240,
        revisions=3,
        finals=2,
    )

    assert _value(
        ASR_STREAM_SESSIONS,
        "asr_stream_sessions_total",
        {"model": model, "status": "disconnected"},
    ) == 1
    assert _value(
        ASR_STREAM_FIRST_RESULT_SECONDS,
        "asr_stream_first_result_seconds_count",
        {"model": model},
    ) == 1
    assert _value(
        ASR_STREAM_REVISIONS,
        "asr_stream_revisions_total",
        {"model": model},
    ) == 3
    assert _value(
        ASR_STREAM_FINALS,
        "asr_stream_finals_total",
        {"model": model},
    ) == 2
