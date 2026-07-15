> **开源说明**：本文由一次私有部署复盘脱敏而来，仅保留部署清单与排障思路。示例中的主机名、内网地址、密钥路径、模型路径均为占位符；请替换为自己的环境配置，不要将真实 IP、SSH 私钥、API Key 或云账号信息提交到仓库。

# 语音大模型后端服务 — 最终部署总结（2026-07-05）

> 数据快照：2026-07-05 | 双机集群实测。本文汇总 ASR/TTS/语音聊天三大能力的最终部署状态、关键配置、以及延迟优化三轮探索（fp16/vLLM/量化/聚合窗）的性能对比与结论。

---

## 1. 集群拓扑（权威快照）

| 项 | node1（入口机） | node2（扩展机） |
| --- | --- | --- |
| hostname | <HOSTNAME> | <HOSTNAME> |
| 内网 IP | <NODE1_PRIVATE_IP> | <NODE2_PRIVATE_IP> |
| 公网 IP（当前） | **<NODE_PUBLIC_HOST>** | **<NODE_PUBLIC_HOST>** |
| GPU | Tesla T4 15360 MiB | Tesla T4 15360 MiB |
| 稳态显存占用 | ~5869 MiB（余 ~9GB） | ~5869 MiB |
| 组件 | web(nginx LB)+backend+grafana+prometheus | backend-only |
| Docker Hub | 可达（常规 compose build） | 不可达（overlay 增量构建） |

> **IP 提示**：node1/node2 公网 IP 曾在实例重启时**互换**（历史快照里的旧 IP 已过时）。以 **内网 IP + hostname + `node_name`** 为准，公网 IP 随实例重启可能再变。

---

## 2. 三大能力最终状态

| 能力 | 端点 | 引擎 | 状态 |
| --- | --- | --- | --- |
| **ASR 文件式** | `POST /api/v1/asr` | SenseVoice / seaco（热词） | ✅ 双机上线 |
| **ASR 真流式 2pass** | `WS /ws/asr` | paraformer-online 出字 + SenseVoice/seaco 定稿 | ✅ 双机上线（480ms 聚合窗） |
| **TTS 文件/流式** | `POST /api/v1/tts` / `WS /ws/tts` | CosyVoice2-0.5B（分句流式） | ✅ 双机上线 |
| **语音聊天 S2S** | `WS /ws/chat` | ASR→远端 Agent(qwen-plus)→TTS | ✅ 双机 LB |

---

## 3. 关键配置（两机一致，实测确认）

| 配置项 | 值 | 说明 |
| --- | --- | --- |
| `asr_engine` | funasr | — |
| `agent_asr_model` | funasr-streaming | 聊天定稿走 2pass |
| **`funasr_streaming_chunk_ms`** | **480** | 聚合窗（600→480 优化，见 §4.4） |
| **`funasr_streaming_chunk_size`** | **[0,8,4]** | 8 帧×60ms=480ms，前看 4 帧 |
| **`funasr_drop_nonspeech_events`** | **True** | 咳嗽等非语音事件过滤（见 §5） |
| `cosyvoice_fp16` | **False** | fp16 实测负面，已关（见 §4.1） |
| `tts_sentence_stream` | True | 分句流式 TTFB 优化 |
| `chat_barge_in` | False（前端可逐连接开） | 说话打断 |
| `chat_min_speech_chars` / `_ms` | 2 / 300 | 噪声门控 |

---

## 4. 延迟优化四轮探索：性能对比与结论

### 4.1 fp16 推理（TTS）— ❌ 负面，已放弃
双机灰度对照（node1 fp16=True vs node2 fp16=False），5 语料 × 20 轮：

| 语料(字数) | fp16 TTFB | 基线 TTFB | 结果 |
| --- | --- | --- | --- |
| 你好(3) | 1510 | 1297 | 慢 16% |
| 今天天气(14) | 3126 | 2887 | 慢 8% |
| 我帮你查(19) | 3554 | 3334 | 慢 7% |
| 人工智能(35) | 4418 | 4134 | 慢 7% |
| 好的这个(50) | 3337 | 4136 | 快 19% |

**5 中 4 条更慢**。根因：T4 上 CosyVoice2 瓶颈在自回归 LM 串行生成，非可 fp16 加速的矩阵算子。→ 两机统一关闭。

### 4.2 vLLM（LLM 加速）— ❌ 评估不推荐
实测 ASR+TTS 稳态占 5.87GB/15GB。vLLM 预分配 KV 池模型与现有常驻显存强冲突（压到 `gpu_memory_utilization≈0.2` 后 KV 池仅 0.8-1.5GB，吞吐优势丧失）+ Turing(SM7.5) 不支持 bf16 + torch 2.3→需升级。收益场景（高并发批处理）与单卡低并发错配。→ 不推荐。详见《T4显存限制下vLLM可行性评估报告》。

### 4.3 量化（GPTQ/AWQ/INT8）— ❌ 评估不推荐
CosyVoice2 LLM 是 Qwen2-0.5B 级（hidden 896/24 层/bf16）。量化解决「显存不足/大模型访存带宽」，而本服务不缺显存（余 9GB）+ 模型极小 + batch=1，量化收益前提全不满足，反量化开销反而可能更慢（与 fp16 同机理）。→ 不推荐。

### 4.4 ASR 聚合窗 600→480ms — ✅ 正面，已双机推全
node1(480) vs node2(600) 对照，同一 70.5s 音频 × 4 轮：

| 指标 | 480ms | 600ms 基线 | 收益 |
| --- | --- | --- | --- |
| **首字延迟中位数** | **1025.9 ms** | 1375.5 ms | **↓349.6 ms（-25.4%）** |
| partial 出字密度 | 138 | 103 | +34% |
| 定稿句数 | 4 | 4 | 断句无退化 |

**字准逐字对照**：480ms 不仅无退化，反而 4 处边界更准（「劳同」→「活动」、「三炮」→「三茂」等），0 处更差。→ 字准确认后从 node1 推全至 node2。

### 4.5 优化探索总结
| 方案 | 命中瓶颈 | 结果 | 成本 |
| --- | --- | --- | --- |
| fp16 | LLM（但 T4 无效） | ❌ 多数更慢 | 一行开关 |
| vLLM | LLM（正中） | ❌ 显存冲突不推荐 | 高 |
| 量化 | 显存（非瓶颈） | ❌ 前提不满足 | 中 |
| **聚合窗 480ms** | **ASR 首字延迟** | ✅ **↓25%，字准不降** | **零成本** |

**核心结论**：TTS 端在单 T4 上无「改开关/换精度」式捷径（瓶颈是架构层的自回归串行生成，需独立 GPU 或换架构）；ASR 端的聚合窗调优是本轮唯一有效、零成本、可回退的延迟优化。

---

## 5. 稳定性修复（本阶段）

| 修复 | 说明 | 状态 |
| --- | --- | --- |
| TTS markdown 误读 | `strip_markdown` 送引擎前剥离 markdown 语法 | ✅ 双机 |
| 环境噪声误触发 | 前端能量 VAD + 后端有效发言门控（≥2字/≥300ms） | ✅ 双机 |
| **咳嗽等非语言声误触发** | SenseVoice 事件标签层（`<\|Cough\|>` 等命中判噪声） | ✅ 双机（本次 build 进镜像） |
| barge-in 说话打断 | 浏览器 AEC + ASR 文字判据，前端可开关 | ✅ 双机 |

### 5.1 部署方式教训（重要）
- **docker cp + restart 的改动会被 `compose up -d`（recreate）冲掉**：本阶段咳嗽事件层最初用 docker cp 进容器，后续 fp16/聚合窗实验的 recreate 将其冲回镜像旧代码，导致事件层一度失效。
- **治本方式**：改动必须 **build 进镜像**——node1 常规 `compose build`，node2 `Dockerfile.node2overlay` 增量构建（`FROM asr-tts-backend:latest`+`COPY app`，零联网）。本次已按此修复，两机 recreate 不会再丢。

---

## 6. 待办 / 遗留

- **Git 未提交**：本阶段全部代码改动（TTS markdown、噪声门控、barge-in、咳嗽事件层）已双机部署且 build 进镜像，但**尚未提交 git**。480ms 聚合窗走 env（未改 config 默认值）。
- **config.py 默认值**：`cosyvoice_fp16=False`、`funasr_streaming_chunk_ms=600`、`funasr_drop_nonspeech_events=True` 为代码默认；480ms 经 node-local `.env.deploy` 注入（新部署未显式配 env 时回落 600ms 保守基线）。
- **env 备份**：两机 `.env.deploy.bak.fp16`、`.env.deploy.bak.chunkwin` 保留可回退。

---

## 附：数据采集方法（可复现，全程只读）
- 配置快照：容器内 `python -c "from app.config import Settings; ..."`。
- 显存：`nvidia-smi --query-gpu`。
- TTS TTFB：`/ws/tts` 客户端首块计时 + 服务端 QoS。
- ASR 首字延迟：`/ws/asr` ~85ms 实时喂音频，测首个 partial 到达时间。
- 字准：同一 vad_example.wav 双机 final 文本逐字对比。
- 代码完整性：容器内 `grep -c '_is_nonspeech_event'`。
