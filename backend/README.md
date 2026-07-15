# 语音大模型后端服务 (ASR + TTS)

阿里系开源模型自托管：FunASR (ASR) + CosyVoice2 (TTS)。
当前实现：Phase 1 MVP — 文件式 + 流式两种交互，五层分层架构。

## 本地开发（无 GPU）

后端默认使用 **stub 引擎**，无需安装模型即可跑通完整链路与前端联调：

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

健康检查：`curl localhost:8000/healthz` / `curl localhost:8000/readyz`

## 本地测试真实模型（macOS CPU/MPS）

一键脚本在 `scripts/`，完整步骤见 [本地真实模型测试指南](../docs/getting-started/local-models.md)：

```bash
# ASR (FunASR / SenseVoice)
bash scripts/setup_funasr.sh
python scripts/download_models.py --asr
python scripts/smoke_test.py --asr --audio your_speech.wav

# TTS (CosyVoice2 零样本音色)
bash scripts/setup_cosyvoice.sh
python scripts/download_models.py --tts
# 准备参考音频后 (见指南 4.3) 配置 .env
python scripts/smoke_test.py --tts
```

`cp scripts/env.local.example .env` 作为真实模型配置起点。

## 自托管真实模型（GPU 环境）

```bash
pip install -e ".[engines]"
export ASR_ENGINE=funasr TTS_ENGINE=cosyvoice DEVICE=cuda
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

模型权重从 ModelScope 下载，路径通过环境变量配置（见 `app/config.py`）。

## 运行测试

```bash
cd backend && ASR_ENGINE=stub TTS_ENGINE=stub CHAT_ENABLED=false pytest
```

## 配置

所有配置见 [app/config.py](app/config.py)，支持环境变量 / `.env` 覆盖。
关键项：`ASR_ENGINE` / `TTS_ENGINE`（`stub` | `funasr` | `cosyvoice`）、模型路径、`TARGET_SAMPLE_RATE`。
