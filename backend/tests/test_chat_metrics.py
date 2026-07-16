from app.observability.metrics import (
    CHAT_AUDIO_SECONDS,
    CHAT_ENDPOINT_SECONDS,
    CHAT_FIRST_AUDIO_SECONDS,
    CHAT_INTERRUPT_SECONDS,
    CHAT_PLAYBACK_DROPPED_CHUNKS,
    CHAT_PLAYBACK_UNDERRUNS,
    CHAT_PROVIDER_ERRORS,
    CHAT_SESSIONS,
    CHAT_TOKENS,
    CHAT_TURNS,
    observe_chat_playback_dropped_chunks,
    observe_chat_playback_underrun,
    observe_chat_provider_error,
    observe_chat_session,
    observe_chat_turn,
    observe_chat_usage,
)


def _value(collector, name: str, labels: dict[str, str]) -> float:
    for metric in collector.collect():
        for sample in metric.samples:
            if sample.name == name and sample.labels == labels:
                return float(sample.value)
    return 0.0


def test_chat_metrics_record_session_turn_audio_and_errors():
    provider = "test-provider"
    model = "test-model"
    base = {"provider": provider, "model": model}

    observe_chat_session(provider, model, "ok")
    observe_chat_turn(
        provider,
        model,
        status="ok",
        endpoint_ms=320,
        first_audio_ms=850,
        total_ms=2200,
        input_audio_ms=1200,
        output_audio_ms=1800,
        interrupt_ms=75,
    )
    observe_chat_provider_error(provider, model, "timeout")
    observe_chat_usage(
        provider,
        model,
        {
            "input_tokens": 12,
            "output_tokens": 9,
            "input_tokens_details": {"text_tokens": 5, "audio_tokens": 7},
            "output_tokens_details": {"text_tokens": 3, "audio_tokens": 6},
        },
    )
    observe_chat_playback_underrun(provider, model, 2)
    observe_chat_playback_dropped_chunks(provider, model, 3)

    assert _value(
        CHAT_SESSIONS, "chat_sessions_total", {**base, "status": "ok"}
    ) == 1
    assert _value(
        CHAT_TURNS, "chat_turns_total", {**base, "status": "ok"}
    ) == 1
    assert _value(
        CHAT_ENDPOINT_SECONDS, "chat_endpoint_seconds_count", base
    ) == 1
    assert _value(
        CHAT_FIRST_AUDIO_SECONDS, "chat_first_audio_seconds_count", base
    ) == 1
    assert _value(
        CHAT_INTERRUPT_SECONDS, "chat_interrupt_seconds_count", base
    ) == 1
    assert _value(
        CHAT_AUDIO_SECONDS,
        "chat_audio_seconds_total",
        {**base, "direction": "input"},
    ) == 1.2
    assert _value(
        CHAT_AUDIO_SECONDS,
        "chat_audio_seconds_total",
        {**base, "direction": "output"},
    ) == 1.8
    assert _value(
        CHAT_PROVIDER_ERRORS,
        "chat_provider_errors_total",
        {**base, "code": "timeout"},
    ) == 1
    assert _value(
        CHAT_TOKENS,
        "chat_tokens_total",
        {**base, "direction": "input", "modality": "audio"},
    ) == 7
    assert _value(
        CHAT_TOKENS,
        "chat_tokens_total",
        {**base, "direction": "output", "modality": "text"},
    ) == 3
    assert _value(
        CHAT_PLAYBACK_UNDERRUNS,
        "chat_playback_underruns_total",
        base,
    ) == 2
    assert _value(
        CHAT_PLAYBACK_DROPPED_CHUNKS,
        "chat_playback_dropped_chunks_total",
        base,
    ) == 3
