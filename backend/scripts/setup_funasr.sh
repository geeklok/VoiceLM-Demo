#!/usr/bin/env bash
# 安装 FunASR (ASR) 真实模型依赖。
# 适用: 本地 macOS (CPU/MPS) 测试 SenseVoiceSmall + FSMN-VAD。
#
# 用法:
#   bash scripts/setup_funasr.sh [venv_dir]
# 默认在 backend/.venv-funasr 建立独立虚拟环境, 避免污染主环境。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # backend/
VENV="${1:-$HERE/.venv-funasr}"

echo "==> FunASR 环境目录: $VENV"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install --upgrade pip wheel

echo "==> 安装 PyTorch (CPU/MPS 版)"
# macOS 上 pip 默认安装的 torch 即带 MPS 支持; 无需 CUDA wheel
python -m pip install "torch>=2.1" torchaudio

echo "==> 安装 FunASR + ModelScope"
python -m pip install "funasr>=1.1.0" "modelscope>=1.13" "huggingface_hub"

echo "==> 安装后端服务依赖 (与引擎共用一个环境)"
python -m pip install -e "$HERE"

cat <<'EOF'

==> FunASR 依赖安装完成。
下一步:
  1. 下载模型:   python scripts/download_models.py --asr
  2. 冒烟测试:   python scripts/smoke_test.py --asr
  3. 启动服务:   见 docs/本地真实模型测试指南.md

提示: 该虚拟环境已 source, 当前 shell 可直接运行上面命令。
新开终端需先: source .venv-funasr/bin/activate
EOF
