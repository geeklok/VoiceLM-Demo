#!/usr/bin/env bash
# 安装 CosyVoice2 (TTS) 真实模型依赖。
# 适用: 本地 macOS (CPU/MPS) 测试 CosyVoice2-0.5B 零样本合成。
#
# CosyVoice 必须从源码仓库运行 (需要 third_party/Matcha-TTS 子模块),
# 不能只 pip install。本脚本会 clone 仓库并建立独立虚拟环境。
#
# 用法:
#   bash scripts/setup_cosyvoice.sh [repo_dir] [venv_dir]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # backend/
REPO="${1:-$HERE/third_party/CosyVoice}"
VENV="${2:-$HERE/.venv-cosyvoice}"

echo "==> CosyVoice 仓库目录: $REPO"
if [ ! -d "$REPO/.git" ]; then
  mkdir -p "$(dirname "$REPO")"
  git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git "$REPO"
else
  echo "    仓库已存在, 更新子模块"
  git -C "$REPO" submodule update --init --recursive
fi

if [ ! -d "$REPO/third_party/Matcha-TTS" ]; then
  echo "!! 未找到 third_party/Matcha-TTS, 重新拉取子模块"
  git -C "$REPO" submodule update --init --recursive
fi

echo "==> CosyVoice 环境目录: $VENV"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install --upgrade pip wheel

echo "==> 安装 PyTorch (CPU/MPS 版)"
python -m pip install "torch>=2.1" torchaudio

echo "==> 安装 CosyVoice 依赖 (requirements.txt)"
# macOS 上 pynini/WeTextProcessing 可能编译困难;
# 失败时见 docs/本地真实模型测试指南.md 的常见问题。
python -m pip install -r "$REPO/requirements.txt" || {
  echo "!! requirements.txt 安装有失败项 (常见: pynini/tensorrt)。"
  echo "   macOS 可先用 conda 装 pynini, 或跳过 GPU-only 依赖, 详见文档。"
}

echo "==> 安装后端服务依赖 (与引擎共用一个环境)"
python -m pip install -e "$HERE" "modelscope>=1.13" "huggingface_hub"

cat <<EOF

==> CosyVoice 依赖安装完成。
仓库路径 (写入 .env 的 COSYVOICE_REPO_DIR):
  $REPO

下一步:
  1. 下载模型:   python scripts/download_models.py --tts
  2. 准备参考音色: 见文档 (一段 3-10s 16k wav + 对应文本)
  3. 冒烟测试:   python scripts/smoke_test.py --tts
EOF
