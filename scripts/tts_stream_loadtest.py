#!/usr/bin/env python3
"""流式 TTS 压测 —— 填充 tts_ttfb_seconds / tts_rtf baseline (Phase 3 §7.1)。

经 WebSocket 打 /ws/tts (流式模式), 触发后端 observe_tts(..., "stream", ...) 上报,
从而把 file 模式下一直为空的 tts_ttfb_seconds 直方图填出 baseline。

WS 协议 (见 backend/app/api/routes_tts.py):
  client -> {"type":"synthesize","text","voice","speed","model"}
  server -> {"type":"meta","sample_rate":N,"format":"pcm_s16le"}
  server -> <binary pcm_s16le chunk> ...
  server -> {"type":"done"}            # 或 {"type":"error",...}

客户端侧也记 TTFB / RTF 作交叉验证 (含网络往返, 会略大于服务端埋点)。
服务端真实 baseline 仍以 Prometheus 的 tts_ttfb_seconds 为准 (网络无关)。

用法:
  # 打公网 LB (默认), 让请求在两机间分流
  python scripts/tts_stream_loadtest.py --url wss://your-server.example.com/ws/tts \
      --concurrency 4 --total 80 --warmup 4

  # 只压某台机器内网 (在该机本地跑, 排除 LB 与公网抖动)
  python scripts/tts_stream_loadtest.py --url ws://127.0.0.1:8000/ws/tts \
      --concurrency 2 --total 40

参数:
  --url          WS 端点 (默认 wss://your-server.example.com/ws/tts)
  --concurrency  并发 worker 数 (默认 4)
  --total        总请求数 (默认 80)
  --warmup       预热请求数, 不计入统计 (默认 4)
  --voice/--speed/--model  透传给后端
  --insecure     跳过 TLS 证书校验 (自签证书默认开启)
  --verbose      打印每条请求明细
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
from dataclasses import dataclass
from typing import Optional

import websockets

# 文本长短交错: TTFB 与文本长度基本无关(只看首块), RTF/总时长随文本增长。
DEFAULT_CORPUS = [
    "你好。",
    "今天天气不错，适合出门散步。",
    "语音合成的首包延迟是衡量流式体验的关键指标。",
    "人工智能正在深刻改变我们获取信息和与机器交互的方式，"
    "其中语音技术扮演着越来越重要的角色。",
    "实时语音合成要求系统在极短时间内返回第一个音频片段，"
    "通常我们希望首包延迟控制在一百五十毫秒左右，"
    "这样用户几乎感受不到等待，对话才会显得自然流畅。",
    "深度学习模型的推理优化是一个系统工程，"
    "涉及批处理、显存管理、算子融合以及量化等多个层面，"
    "每一项改进都可能带来吞吐与延迟上的显著收益，"
    "而可观测性则是这一切优化能够被量化评估的前提。",
]

# 多句语料: 每条含多个句末标点且首句极短, 用于验证/量化分句流式 TTFB 收益。
# (DEFAULT_CORPUS 多为单句, 分句器按设计不触发 -> 等价基线; 见 Runbook §6.5)
MULTI_SENTENCE_CORPUS = [
    "你好。今天天气很好。我们一起去公园散步吧。路上可以聊聊最近的新闻。然后再找家餐厅吃饭。",
    "好的。这个问题我来解释一下。首先要理解它的背景。其次是具体的实现方式。最后我们看几个例子。",
    "早上好。会议改到下午三点了。地点在二楼会议室。记得带上你的笔记本。我们准时开始。",
    "明白了。我先确认一下需求。然后给出初步方案。如果没问题就进入开发。预计本周内交付。",
    "嗯。这道菜其实很简单。先把食材洗净切好。再热油下锅翻炒。最后调味出锅就可以了。",
    "可以。我来总结今天的要点。第一是进度符合预期。第二是风险已经识别。第三是下一步计划已定。",
]


@dataclass
class Result:
    ok: bool
    status: str = "ok"            # ok / error / busy / timeout / conn_fail
    ttfb_ms: Optional[float] = None      # send -> 首个二进制块
    meta_ms: Optional[float] = None      # send -> meta
    total_ms: Optional[float] = None     # send -> done
    audio_ms: Optional[float] = None
    rtf: Optional[float] = None
    bytes_recv: int = 0
    text_len: int = 0
    node: str = ""                # 本次请求被 LB 分到的机器 (meta.node)
    model: str = ""


async def one_request(url: str, ssl_ctx, text: str, voice: str,
                      speed: float, model: Optional[str]) -> Result:
    payload = {"type": "synthesize", "text": text, "voice": voice, "speed": speed}
    if model:
        payload["model"] = model
    try:
        async with websockets.connect(
            url, ssl=ssl_ctx, max_size=None, open_timeout=15, close_timeout=5,
            ping_interval=None,
        ) as ws:
            t_send = time.perf_counter()
            await ws.send(json.dumps(payload))

            sr: Optional[int] = None
            t_meta = t_first = None
            nbytes = 0
            node = ""
            mdl = ""
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    return Result(False, "timeout", text_len=len(text))

                if isinstance(msg, (bytes, bytearray)):
                    if t_first is None:
                        t_first = time.perf_counter()
                    nbytes += len(msg)
                    continue

                obj = json.loads(msg)
                mtype = obj.get("type")
                if mtype == "meta":
                    sr = int(obj.get("sample_rate") or 0)
                    node = obj.get("node", "") or ""
                    mdl = obj.get("model", "") or ""
                    t_meta = time.perf_counter()
                elif mtype == "done":
                    t_done = time.perf_counter()
                    samples = nbytes // 2  # pcm_s16le -> 2 bytes/sample
                    audio_ms = (samples / sr * 1000) if sr else None
                    total_ms = (t_done - t_send) * 1000
                    return Result(
                        ok=True,
                        ttfb_ms=(t_first - t_send) * 1000 if t_first else None,
                        meta_ms=(t_meta - t_send) * 1000 if t_meta else None,
                        total_ms=total_ms,
                        audio_ms=audio_ms,
                        rtf=(total_ms / audio_ms) if audio_ms else None,
                        bytes_recv=nbytes,
                        text_len=len(text),
                        node=node,
                        model=mdl,
                    )
                elif mtype == "error":
                    code = obj.get("code")
                    return Result(False, "busy" if code == "busy" else "error",
                                  text_len=len(text))
                else:
                    return Result(False, "error", text_len=len(text))
    except Exception:  # noqa: BLE001
        return Result(False, "conn_fail", text_len=len(text))


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q / 100 * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * frac


async def run(args) -> None:
    ssl_ctx = None
    if args.url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if args.insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    corpus = MULTI_SENTENCE_CORPUS if args.corpus == "multi" else DEFAULT_CORPUS
    counter = {"i": 0}
    lock = asyncio.Lock()
    results: list[Result] = []
    warmup_done = {"n": 0}

    total = args.total + args.warmup
    wall_start = time.perf_counter()

    async def worker():
        while True:
            async with lock:
                idx = counter["i"]
                if idx >= total:
                    return
                counter["i"] += 1
            text = corpus[idx % len(corpus)]
            r = await one_request(args.url, ssl_ctx, text, args.voice,
                                  args.speed, args.model)
            is_warmup = idx < args.warmup
            tag = "warmup" if is_warmup else "measure"
            if args.verbose:
                print(f"[{idx:>3}/{total}] {tag:<7} {r.status:<9} "
                      f"ttfb={_fmt(r.ttfb_ms)}ms total={_fmt(r.total_ms)}ms "
                      f"rtf={_fmt(r.rtf,3)} bytes={r.bytes_recv} len={r.text_len}")
            if not is_warmup:
                results.append(r)

    await asyncio.gather(*[worker() for _ in range(args.concurrency)])
    wall = time.perf_counter() - wall_start

    _report(results, wall, args)


def _fmt(v, nd=1):
    return "-" if v is None else f"{v:.{nd}f}"


def _report(results: list[Result], wall: float, args) -> None:
    ok = [r for r in results if r.ok]
    n = len(results)
    n_ok = len(ok)
    busy = sum(1 for r in results if r.status == "busy")
    err = sum(1 for r in results if r.status in ("error", "conn_fail", "timeout"))

    ttfb = [r.ttfb_ms for r in ok if r.ttfb_ms is not None]
    rtf = [r.rtf for r in ok if r.rtf is not None]
    total_ms = [r.total_ms for r in ok if r.total_ms is not None]

    print("\n" + "=" * 60)
    print("流式 TTS 压测结果 (客户端侧测量, 含网络往返)")
    print("=" * 60)
    print(f"URL          : {args.url}")
    print(f"并发 / 总数   : {args.concurrency} / {args.total} (warmup {args.warmup})")
    print(f"墙钟耗时      : {wall:.1f}s   吞吐: {n_ok / wall:.2f} req/s")
    print(f"成功 / 限流429 / 失败 : {n_ok} / {busy} / {err}  (共 {n})")
    if ttfb:
        print("-" * 60)
        print("TTFB 首包延迟 (ms):")
        print(f"  p50={pct(ttfb,50):.0f}  p90={pct(ttfb,90):.0f}  "
              f"p95={pct(ttfb,95):.0f}  p99={pct(ttfb,99):.0f}  max={max(ttfb):.0f}")
    if rtf:
        print("-" * 60)
        print("RTF 实时率 (合成总耗时/音频时长, <1 才实时):")
        print(f"  p50={pct(rtf,50):.3f}  p90={pct(rtf,90):.3f}  "
              f"p95={pct(rtf,95):.3f}  max={max(rtf):.3f}")
    if total_ms:
        print("-" * 60)
        print("端到端总耗时 (ms):")
        print(f"  p50={pct(total_ms,50):.0f}  p95={pct(total_ms,95):.0f}  "
              f"max={max(total_ms):.0f}")

    # 按机器拆分: 灰度对比的核心 —— node1 (优化版) vs node2 (基线版)。
    nodes = sorted({r.node for r in ok if r.node})
    if nodes:
        print("-" * 60)
        print("按机器拆分 (LB 分流 + 灰度对比):")
        for nd in nodes:
            sub = [r for r in ok if r.node == nd]
            sub_ttfb = [r.ttfb_ms for r in sub if r.ttfb_ms is not None]
            sub_rtf = [r.rtf for r in sub if r.rtf is not None]
            mdl = next((r.model for r in sub if r.model), "")
            line = f"  {nd:<8} n={len(sub):<3} model={mdl or '-'}"
            if sub_ttfb:
                line += (f"  TTFB p50={pct(sub_ttfb,50):.0f} "
                         f"p95={pct(sub_ttfb,95):.0f}ms")
            if sub_rtf:
                line += f"  RTF p50={pct(sub_rtf,50):.3f}"
            print(line)
    print("=" * 60)
    print("注: 服务端真实 baseline 看 Prometheus tts_ttfb_seconds (网络无关)。")
    print("    PromQL: histogram_quantile(0.95, sum(rate(tts_ttfb_seconds_bucket[5m])) by (le, node))")


def main() -> None:
    p = argparse.ArgumentParser(description="流式 TTS 压测填充 TTFB baseline")
    p.add_argument("--url", default="wss://your-server.example.com/ws/tts")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--total", type=int, default=80)
    p.add_argument("--warmup", type=int, default=4)
    p.add_argument("--corpus", choices=["default", "multi"], default="default",
                   help="default=单句 baseline 语料; multi=多句语料(验证分句流式)")
    p.add_argument("--voice", default="中文女")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--model", default=None)
    p.add_argument("--insecure", action="store_true", default=True,
                   help="跳过 TLS 校验 (自签证书, 默认开启)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
