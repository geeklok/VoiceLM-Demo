#!/usr/bin/env python3
"""用同一批 16kHz 单声道 WAV 对比级联与原生语音聊天。"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import websockets


@dataclass
class Case:
    id: str
    path: Path
    tags: list[str]


@dataclass
class Result:
    case_id: str
    tags: list[str]
    mode: str
    model: str
    round: int
    ok: bool
    status: str
    endpoint_ms: Optional[float] = None
    first_audio_ms: Optional[float] = None
    total_ms: Optional[float] = None
    audio_ms: Optional[float] = None
    user_text: str = ""
    assistant_text: str = ""
    node: str = ""
    provider: str = ""
    server_qos: Optional[dict] = None


def load_manifest(path: Path) -> list[Case]:
    body = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    cases = []
    for item in body:
        wav_path = Path(item["path"])
        if not wav_path.is_absolute():
            wav_path = base / wav_path
        cases.append(
            Case(
                id=str(item["id"]),
                path=wav_path,
                tags=[str(tag) for tag in item.get("tags", [])],
            )
        )
    if not cases:
        raise ValueError("语料清单为空")
    return cases


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1:
            raise ValueError(f"{path}: 必须是单声道")
        if wav.getframerate() != 16000:
            raise ValueError(f"{path}: 必须是 16kHz")
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path}: 必须是 16-bit PCM WAV")
        data = wav.readframes(wav.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


async def run_one(
    args: argparse.Namespace,
    ssl_ctx: Optional[ssl.SSLContext],
    case: Case,
    mode: str,
    model: str,
    round_no: int,
) -> Result:
    pcm = load_wav(case.path)
    chunk_samples = max(160, int(16000 * args.chunk_ms / 1000))
    tail_chunks = max(1, int(args.tail_silence_ms / args.chunk_ms))
    result = Result(
        case_id=case.id,
        tags=case.tags,
        mode=mode,
        model=model,
        round=round_no,
        ok=False,
        status="started",
    )
    timestamps: dict[str, float] = {}
    done = asyncio.Event()
    audio_bytes = 0

    try:
        async with websockets.connect(
            args.url,
            ssl=ssl_ctx,
            max_size=None,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "mode": mode,
                        "model": model,
                        "sample_rate": 16000,
                        "channels": 1,
                        "input_format": "pcm_f32le",
                        "language": "auto",
                        "barge_in": False,
                        "enable_thinking": False,
                    }
                )
            )
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(msg, bytes):
                    continue
                event = json.loads(msg)
                if event.get("type") == "ready":
                    result.node = str(event.get("node") or "")
                    result.provider = str(event.get("provider") or mode)
                    break
                if event.get("type") == "error":
                    result.status = str(event.get("code") or "error")
                    return result

            async def receive() -> None:
                nonlocal audio_bytes
                try:
                    while True:
                        msg = await ws.recv()
                        now = time.perf_counter()
                        if isinstance(msg, bytes):
                            if "first_audio" not in timestamps:
                                timestamps["first_audio"] = now
                            audio_bytes += len(msg)
                            continue
                        event = json.loads(msg)
                        event_type = event.get("type")
                        if event_type == "state" and event.get("state") == "thinking":
                            timestamps.setdefault("endpoint", now)
                        elif event_type == "user_final":
                            result.user_text = str(event.get("text") or "")
                        elif event_type == "tts_meta":
                            result.node = str(event.get("node") or result.node)
                            result.provider = str(
                                event.get("provider") or result.provider
                            )
                        elif event_type == "assistant_done":
                            result.assistant_text = str(event.get("text") or "")
                            result.server_qos = event.get("qos")
                            timestamps["done"] = now
                            result.status = "ok"
                            result.ok = True
                            done.set()
                            return
                        elif event_type == "interrupted":
                            result.status = "interrupted"
                            done.set()
                            return
                        elif event_type == "error":
                            result.status = str(event.get("code") or "error")
                            done.set()
                            return
                except Exception as exc:  # noqa: BLE001
                    if not done.is_set():
                        result.status = f"receive:{type(exc).__name__}"
                        done.set()

            recv_task = asyncio.create_task(receive())
            chunk_s = args.chunk_ms / 1000
            for offset in range(0, len(pcm), chunk_samples):
                await ws.send(
                    pcm[offset : offset + chunk_samples].astype("<f4").tobytes()
                )
                await asyncio.sleep(chunk_s)
            timestamps["speech_end"] = time.perf_counter()

            silence = np.zeros(chunk_samples, dtype="<f4").tobytes()
            for _ in range(tail_chunks):
                await ws.send(silence)
                await asyncio.sleep(chunk_s)

            try:
                await asyncio.wait_for(done.wait(), timeout=args.turn_timeout)
            except asyncio.TimeoutError:
                result.status = "timeout"
            finally:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):
                    pass

            try:
                await ws.send(json.dumps({"type": "end"}))
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        result.status = f"connection:{type(exc).__name__}"

    speech_end = timestamps.get("speech_end")
    if speech_end is not None:
        if "endpoint" in timestamps:
            result.endpoint_ms = (timestamps["endpoint"] - speech_end) * 1000
        if "first_audio" in timestamps:
            result.first_audio_ms = (
                timestamps["first_audio"] - speech_end
            ) * 1000
        if "done" in timestamps:
            result.total_ms = (timestamps["done"] - speech_end) * 1000
    result.audio_ms = audio_bytes / 2 / 24000 * 1000
    return result


async def run(args: argparse.Namespace) -> None:
    ssl_ctx = None
    if args.url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if args.insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    cases = load_manifest(Path(args.manifest))
    modes = [
        ("cascade", args.cascade_model),
        ("native", args.native_model),
    ]
    results: list[Result] = []
    for round_no in range(1, args.rounds + 1):
        for case_index, case in enumerate(cases):
            ordered = modes if (round_no + case_index) % 2 else list(reversed(modes))
            for mode, model in ordered:
                result = await run_one(
                    args, ssl_ctx, case, mode, model, round_no
                )
                results.append(result)
                print(
                    f"[{round_no}/{args.rounds}] {case.id:<24} {mode:<7} "
                    f"{result.status:<18} first_audio={fmt(result.first_audio_ms)}ms "
                    f"total={fmt(result.total_ms)}ms"
                )

    report(results)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n明细已写入: {output}")


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def report(results: list[Result]) -> None:
    print("\n" + "=" * 76)
    print("Chat A/B 结果（客户端计时，用户语料发送完成 -> 首声/完成）")
    print("=" * 76)
    summary: dict[str, dict[str, float]] = {}
    for mode in ("cascade", "native"):
        rows = [item for item in results if item.mode == mode]
        ok = [item for item in rows if item.ok]
        first_audio = [
            item.first_audio_ms for item in ok if item.first_audio_ms is not None
        ]
        total = [item.total_ms for item in ok if item.total_ms is not None]
        summary[mode] = {
            "success_rate": len(ok) / len(rows) if rows else 0,
            "first_audio_p50": percentile(first_audio, 50),
            "first_audio_p95": percentile(first_audio, 95),
            "total_p50": percentile(total, 50),
        }
        print(
            f"{mode:<8} success={len(ok)}/{len(rows)} "
            f"first_audio p50/p95={fmt(summary[mode]['first_audio_p50'])}/"
            f"{fmt(summary[mode]['first_audio_p95'])}ms "
            f"total p50={fmt(summary[mode]['total_p50'])}ms"
        )
    cascade = summary["cascade"]["first_audio_p50"]
    native = summary["native"]["first_audio_p50"]
    if cascade and not np.isnan(cascade) and not np.isnan(native):
        gain = (cascade - native) / cascade * 100
        print(f"原生语音首声 p50 相对级联: {gain:+.1f}%")


def fmt(value: Optional[float]) -> str:
    return "-" if value is None or np.isnan(value) else f"{value:.1f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/chat")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cascade-model", required=True)
    parser.add_argument("--native-model", required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--tail-silence-ms", type=int, default=1200)
    parser.add_argument("--turn-timeout", type=float, default=90)
    parser.add_argument("--output")
    parser.add_argument("--insecure", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
