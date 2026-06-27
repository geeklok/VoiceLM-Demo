#!/usr/bin/env python3
"""从 ModelScope 下载 FunASR / CosyVoice 模型权重到本地。

用法:
    python scripts/download_models.py --asr          # 仅 ASR (SenseVoice + VAD)
    python scripts/download_models.py --tts          # 仅 TTS (CosyVoice2-0.5B)
    python scripts/download_models.py --all          # 全部
    python scripts/download_models.py --tts --sft    # 额外下载 SFT spk2info (内置音色, 可选)

下载目录默认 backend/models/<model_id>; 可用 --dest 覆盖。
依赖 modelscope: pip install modelscope
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

ASR_MODELS = [
    "iic/SenseVoiceSmall",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
]
TTS_MODELS = [
    "iic/CosyVoice2-0.5B",
]
# 可选: 内置 SFT 音色 (本服务默认走 zero-shot, 不需要)
SFT_MODELS = [
    "iic/CosyVoice-300M-SFT",
]


def download(model_id: str, dest_root: str) -> str:
    from modelscope import snapshot_download

    local_dir = os.path.join(dest_root, model_id.replace("/", "__"))
    print(f"==> 下载 {model_id} -> {local_dir}")
    path = snapshot_download(model_id, local_dir=local_dir)
    print(f"    完成: {path}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 ASR/TTS 模型权重")
    ap.add_argument("--asr", action="store_true", help="下载 FunASR (SenseVoice + VAD)")
    ap.add_argument("--tts", action="store_true", help="下载 CosyVoice2-0.5B")
    ap.add_argument("--sft", action="store_true", help="额外下载 SFT 内置音色 (可选)")
    ap.add_argument("--all", action="store_true", help="下载全部 (ASR + TTS)")
    ap.add_argument("--dest", default=DEFAULT_DEST, help=f"下载目录 (默认 {DEFAULT_DEST})")
    args = ap.parse_args()

    if not (args.asr or args.tts or args.all or args.sft):
        ap.print_help()
        return 1

    os.makedirs(args.dest, exist_ok=True)
    targets: list[str] = []
    if args.all or args.asr:
        targets += ASR_MODELS
    if args.all or args.tts:
        targets += TTS_MODELS
    if args.sft:
        targets += SFT_MODELS

    paths = {}
    for m in targets:
        paths[m] = download(m, args.dest)

    print("\n==> 全部完成。请把以下路径写入 backend/.env:")
    for m, p in paths.items():
        if m == "iic/SenseVoiceSmall":
            print(f"  FUNASR_MODEL={p}")
        elif m.endswith("fsmn_vad_zh-cn-16k-common-pytorch"):
            print(f"  FUNASR_VAD_MODEL={p}")
        elif m == "iic/CosyVoice2-0.5B":
            print(f"  COSYVOICE_MODEL={p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
