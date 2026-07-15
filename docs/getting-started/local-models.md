> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 本地真实模型测试指南（FunASR + CosyVoice2）

本文档指导你在**本地 macOS（无 NVIDIA GPU，CPU/MPS）**环境下，把后端从 `stub` 占位引擎切换到**真实模型**，验证 FunASR（ASR）与 CosyVoice2（TTS）的效果。

> 适用场景：本地体验真实模型质量、调通链路。生产/公网部署请用 GPU 实例（见技术方案文档 M4）。

---

## 0. 结论先行（最省事的路径）

1. ASR 与 TTS 依赖差异较大，**分别建独立虚拟环境**，互不干扰：
   - FunASR：`scripts/setup_funasr.sh` → `.venv-funasr`
   - CosyVoice：`scripts/setup_cosyvoice.sh` → `.venv-cosyvoice` + clone 仓库
2. 先各自跑 `scripts/smoke_test.py` 冒烟，确认模型能加载、能出结果，**再**起服务。
3. 本地先用 `DEVICE=cpu`（最稳），跑通后再尝试 `mps`。
4. **CosyVoice2 用零样本（zero-shot）音色**：你需要准备一段 3–10 秒、清晰、16k 的参考音频 + 它对应的文字，服务启动时会预注册为命名音色。

---

## 1. 环境要求

| 项 | 要求 | 本机现状 |
|---|---|---|
| OS | macOS | ✅ |
| Python | 3.9–3.11（CosyVoice 官方推荐 3.10） | 系统 3.9.6 |
| ffmpeg | 已安装（音频解码） | ✅ 8.0 |
| 磁盘 | ≥ 8 GB（模型权重约 5–6 GB） | — |
| 内存 | ≥ 8 GB（CPU 推理） | — |
| GPU | 无（用 CPU/MPS） | 无 |

> ⚠️ **Python 版本提醒**：CosyVoice 的部分依赖（`pynini`/`WeTextProcessing`）在 Python 3.9 + macOS 上编译较麻烦。若卡住，建议用 `conda create -n cosyvoice python=3.10` 单独建环境（见第 7 节常见问题）。FunASR 对 3.9 兼容较好。

---

## 2. 目录与脚本一览

```
backend/
├── scripts/
│   ├── setup_funasr.sh        # 装 FunASR 依赖 (独立 venv)
│   ├── setup_cosyvoice.sh     # clone CosyVoice 仓库 + 装依赖 (独立 venv)
│   ├── download_models.py     # 从 ModelScope 下载权重到 backend/models/
│   ├── smoke_test.py          # 不经 HTTP 直接调引擎冒烟
│   └── env.local.example      # 真实模型 .env 模板
├── models/                    # (下载后生成) 模型权重
├── third_party/CosyVoice/     # (clone 后生成) CosyVoice 源码仓库
└── voices/                    # 你自己放参考音频的目录
```

---

## 3. ASR：FunASR（SenseVoiceSmall）

### 3.1 安装

```bash
cd backend
bash scripts/setup_funasr.sh
# 完成后当前 shell 已激活 .venv-funasr
```

### 3.2 下载模型

```bash
python scripts/download_models.py --asr
# 输出会提示把本地路径写进 .env, 例如:
#   FUNASR_MODEL=.../models/iic__SenseVoiceSmall
#   FUNASR_VAD_MODEL=.../models/iic__speech_fsmn_vad_zh-cn-16k-common-pytorch
```

### 3.3 配置

```bash
cp scripts/env.local.example .env
# 编辑 .env: 把 ASR_ENGINE=funasr, FUNASR_MODEL / FUNASR_VAD_MODEL 改成上一步的本地路径
# 本地先用 DEVICE=cpu
```

### 3.4 冒烟测试

```bash
# 不带音频: 用内置正弦波 (验证不报错)
python scripts/smoke_test.py --asr
# 带真实音频 (推荐): 任意采样率/声道, 脚本会自动重采样
python scripts/smoke_test.py --asr --audio /path/to/your_speech.wav
```
看到 `==> ASR 冒烟测试通过 ✅` 且打印出转写文本即成功。

---

## 4. TTS：CosyVoice2-0.5B（零样本音色）

### 4.1 安装（含 clone 仓库）

```bash
cd backend
bash scripts/setup_cosyvoice.sh
# 末尾打印的 "仓库路径" 即 .env 里的 COSYVOICE_REPO_DIR
```

### 4.2 下载模型

```bash
python scripts/download_models.py --tts
# 提示: COSYVOICE_MODEL=.../models/iic__CosyVoice2-0.5B
```

### 4.3 准备参考音色（关键）

CosyVoice2-0.5B 原生只做**零样本克隆**：给一段参考音频 + 它的文字，模型即用该音色合成任意文本。

1. 录制或找一段 **3–10 秒、清晰、无背景音乐**的人声，转成 16k 单声道 wav；需要「中文男」真正是男声时，必须单独准备男声参考音频：
   ```bash
   mkdir -p voices
   ffmpeg -i raw.m4a -ar 16000 -ac 1 voices/default.wav
   ffmpeg -i raw_male.m4a -ar 16000 -ac 1 voices/male.wav
   ```
2. 在 `.env` 里登记该音色（音色名 → 音频路径 + 准确文字）：
   ```
   COSYVOICE_VOICES='{"中文女": {"audio_path": "./voices/default.wav", "prompt_text": "这里填女声参考音频准确的文字内容"}, "中文男": {"audio_path": "./voices/male.wav", "prompt_text": "这里填男声参考音频准确的文字内容"}}'
   COSYVOICE_DEFAULT_VOICE=中文女
   ```
   `prompt_text` 必须与对应参考音频逐字一致；音色名就是前端下拉里的可选项。

> 服务启动/预热时会用 `add_zero_shot_spk` 把这些参考音色注册进模型，之后合成只按音色名调用，不必每次重传音频。

### 4.4 配置

```bash
# 若还没复制过模板:
cp scripts/env.local.example .env
# 编辑 .env: TTS_ENGINE=cosyvoice
#   COSYVOICE_REPO_DIR=./third_party/CosyVoice
#   COSYVOICE_MODEL=<下载路径>
#   COSYVOICE_VOICES=<上一步的 JSON>
```

### 4.5 冒烟测试

```bash
python scripts/smoke_test.py --tts --out smoke_tts_out.wav
# 成功后用播放器打开 smoke_tts_out.wav 听效果
```

---

## 5. 启动服务（真实模型）

ASR 与 TTS 在**不同虚拟环境**时，本地通常分两种玩法：

- **只测一个**：在对应 venv 里把另一个引擎设为 `stub`，单独起服务。
  ```bash
  # 例: 在 .venv-funasr 里只测 ASR
  # .env: ASR_ENGINE=funasr  TTS_ENGINE=stub
  source .venv-funasr/bin/activate
  uvicorn app.main:app --host 127.0.0.1 --port 8000
  ```
- **两个都测**：把 FunASR 和 CosyVoice 装进**同一个** venv（可行但依赖更重），两者都设为真实引擎再起服务。

前端联调：
```bash
cd ../frontend && npm run dev
# 打开 http://localhost:5173, 顶部应显示 "服务就绪"
```

> 首次请求会触发模型加载，**CPU 上较慢**（SenseVoice 数秒、CosyVoice 数十秒级），属正常；预热后变快。

---

## 6. 设备选择：cpu vs mps

| DEVICE | 说明 | 建议 |
|---|---|---|
| `cpu` | 最稳，所有算子都支持 | **先用这个跑通** |
| `mps` | Apple Silicon GPU，部分算子未实现会报错或回退 | 跑通后再试，遇错回退 cpu |

切换：改 `.env` 的 `DEVICE=`，重启服务。若 `mps` 报 `not implemented for MPS`，可设环境变量回退：
```bash
export PYTORCH_ENABLE_MPS_FALLBACK=1
```

---

## 7. 常见问题（macOS）

**Q1. `pip install pynini` / `WeTextProcessing` 失败（CosyVoice 依赖）**
- 优先用 conda：`conda install -y -c conda-forge pynini==2.1.5`，再 `pip install WeTextProcessing --no-deps`。
- 或直接用 Python 3.10 的 conda 环境跑 CosyVoice。

**Q2. `No module named 'matcha'` / `cosyvoice`**
- 确认 `.env` 的 `COSYVOICE_REPO_DIR` 指向 clone 出来的仓库根目录，且 `third_party/Matcha-TTS` 存在（`git submodule update --init --recursive`）。引擎会自动把这两个路径加进 `sys.path`。

**Q3. CosyVoice 合成报 “无可用音色”**
- 没配置 `COSYVOICE_VOICES`，或参考音频路径不存在 / 不是 wav。按第 4.3 节准备。

**Q4. ModelScope 下载慢或中断**
- 重跑 `download_models.py` 会续传。也可手动 `git clone https://www.modelscope.cn/iic/CosyVoice2-0.5B.git` 到 `models/` 下并把路径填进 `.env`。

**Q5. `No module named pytest`**
- 测试依赖：`pip install -e .[dev]`。

**Q6. SenseVoice 转写中文出现富文本标记（如表情/事件标签）**
- 引擎已用 `rich_transcription_postprocess` 清洗；若仍有残留，确认 `funasr` 版本 ≥ 1.1。

**Q7. MPS 偶发 `not implemented`**
- 见第 6 节，设 `PYTORCH_ENABLE_MPS_FALLBACK=1` 或回退 `cpu`。

---

## 8. 验收清单

- [ ] `smoke_test.py --asr` 打印转写文本并通过
- [ ] `smoke_test.py --tts` 生成可播放的 wav 并通过
- [ ] 服务启动后 `/readyz` 返回 ready，前端显示“服务就绪”
- [ ] 前端文件式 ASR 上传音频能得到真实转写
- [ ] 前端 TTS 输入文本能合成并播放真实音色
