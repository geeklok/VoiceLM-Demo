> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 语音合成（TTS / CosyVoice2）服务技术方案

> 状态：**已落地并上线**（双机 node1/node2）| 创建：2026-07-07 | 技术栈：Python(FastAPI) + React/TS，推理引擎 CosyVoice2-0.5B（阿里通义），单卡 Tesla T4（15GB）
>
> 定位：本服务三大业务能力（**ASR / TTS / 语音聊天**）之一。TTS 提供「文本 → 语音」合成，对外暴露两种调用方式：一次性合成（REST，返回完整 WAV）与流式合成（WebSocket，边合成边下发 PCM）。TTS 同时是语音聊天（speech-to-speech）回复播放的底层能力（见《语音聊天-语音到语音对话方案》）。
>
> 本文档是 TTS 能力的**顶层方案索引**，串联既有的分散记录：分句流式 TTFB 优化、markdown 前处理、fp16/vLLM/TensorRT 延迟评估（详见附录指针）。属主技术方案《语音大模型后端服务-技术方案与实施计划》。

---

## 1. 能力概述与设计边界

### 1.1 提供什么

| 能力 | 入口 | 返回 | 典型场景 |
|---|---|---|---|
| 一次性合成 | `POST /api/v1/tts` | 完整 `audio/wav`（body）+ QoS（响应头） | 短文本、需要完整文件下载、**需要变速** |
| 流式合成 | `WS /ws/tts` | `meta` 帧 → 若干裸 int16 PCM 二进制帧 → `done` 帧（含 QoS） | 长文本、低首包延迟、边合成边播 |
| 聊天回复播放 | `WS /ws/chat`（复用 `dispatcher.tts_stream`） | 同流式 | 语音聊天的 AI 回复逐句合成 |

### 1.2 模型选型

- **CosyVoice2-0.5B**（`iic/CosyVoice2-0.5B`），阿里通义开源。原生支持流式合成、zero-shot 音色克隆、fp16 / vLLM / TensorRT 加载开关。
- **音色策略**：CosyVoice2-0.5B 原生只暴露 zero-shot 克隆（`inference_zero_shot`），内置 SFT 音色需另下载 `CosyVoice-300M-SFT` 的 `spk2info.pt`（不稳定，不在范围内）。因此本服务在**加载时**把配置里的「参考音频 + 对应文本」用 `add_zero_shot_spk` 预注册为**命名音色**，合成时只传 `zero_shot_spk_id`——既稳定又避免每次重复抽声纹特征。见 [cosyvoice_engine.py:67-80](../../backend/app/engines/cosyvoice_engine.py#L67-L80)。
- **输出**：24000 Hz 单声道 float32 PCM（`output_sample_rate=24000`，实际以模型 `sample_rate` 为准）。

### 1.3 明确不做（范围裁剪）

- ❌ 流式变速：CosyVoice2 流式不做插值，`synthesize_stream` 内 speed 被写死 1.0（见 §5.3，已知固有限制）。
- ❌ 跨句韵律 / crossfade 拼接：分句流式逐句独立合成，接受轻微句间接缝（避免过度设计）。
- ❌ 情绪 / 动态多音色切换：固定用预注册音色。
- ❌ fp16 / vLLM / TensorRT：均经评估**不采纳**（T4 环境无正收益或改造成本过高，见附录）。

---

## 2. 整体链路

### 2.1 分层结构

```
┌── 客户端 ────────────────────────────────────────────────┐
│  TtsPage(独立页): 一次性合成按钮 / 流式合成按钮            │
│  ChatPage(语音聊天): 逐句流式(复用 tts_stream)            │
└──────────────┬───────────────────────┬───────────────────┘
   POST /api/v1/tts              WS /ws/tts  (WS /ws/chat 内部复用)
               │                           │
┌── L1 路由 (routes_tts.py) ──────────────────────────────┐
│  tts_file: 校验 TTSRequest → dispatcher.tts_file        │
│  tts_stream: 收 synthesize 首帧 → dispatcher.tts_stream │
│             → meta 帧 → PCM 二进制帧* → done 帧(QoS)     │
└──────────────┬──────────────────────────────────────────┘
               │
┌── L2 编排 (dispatcher.py) ──────────────────────────────┐
│  tts_file / tts_stream:                                 │
│   1) strip_markdown(text)        (前处理: 去 md 语法)    │
│   2) apply_domain_tn / apply_default_tts_tn (薄 TN)      │
│   3) [仅 stream] split_sentences (分句降 TTFB, 可开关)   │
│   4) async with limiter.tts_slot()  (单卡 GPU 并发闸)   │
│   5) engine.synthesize / synthesize_stream              │
│   6) 计 QoS (ttfb/process/audio/rtf) + observe_tts 指标 │
└──────────────┬──────────────────────────────────────────┘
               │
┌── L3 引擎 (cosyvoice_engine.py) ────────────────────────┐
│  同步生成器 inference_zero_shot(stream=T/F)              │
│  用 线程 + asyncio.Queue 桥接到异步 (见 §5.2)            │
└──────────────────────────────────────────────────────────┘
```

### 2.2 关键文件

| 层 | 文件 | 职责 |
|---|---|---|
| L1 路由 | [routes_tts.py](../../backend/app/api/routes_tts.py) | REST + WS 两个端点、WS 帧契约 |
| L1 模型发现 | [routes_health.py:54-63](../../backend/app/api/routes_health.py#L54-L63) | `/api/v1/models` 暴露 TTS 引擎名 + 音色列表 |
| L2 编排 | [dispatcher.py:157-250](../../backend/app/orchestration/dispatcher.py#L157-L250) | `tts_file` / `tts_stream`：前处理、限流、QoS |
| L2 并发闸 | [limiter.py:70-71](../../backend/app/orchestration/limiter.py#L70-L71) | `tts_slot()` 单卡 GPU 信号量 |
| L2 前处理 | [text_clean.py](../../backend/app/utils/text_clean.py)、[domain_tn.py](../../backend/app/utils/domain_tn.py) | `strip_markdown`（无条件生效）、领域 TN 薄层、纯数字中文兜底 |
| L2 分句 | [text_split.py:19-47](../../backend/app/utils/text_split.py#L19-L47) | `split_sentences`（流式 TTFB 优化，可开关） |
| L3 引擎契约 | [base.py:67-88](../../backend/app/engines/base.py#L67-L88) | `TTSEngine.synthesize / synthesize_stream` |
| L3 CosyVoice | [cosyvoice_engine.py](../../backend/app/engines/cosyvoice_engine.py) | 模型加载、音色注册、同步→异步桥接 |
| L3 stub | [stub_engine.py](../../backend/app/engines/stub_engine.py) | 本地无 GPU 的正弦占位（测试/本地开发） |
| 编码 | [audio_encode.py](../../backend/app/postprocess/audio_encode.py) | float32 PCM → WAV / 裸 int16 |
| 契约 | [models.py:25-30](../../backend/app/schemas/models.py#L25-L30) | `TTSRequest`（text/voice/speed/format/model） |

---

## 3. 接口契约

### 3.1 一次性合成 `POST /api/v1/tts`

请求体（[TTSRequest](../../backend/app/schemas/models.py#L25-L30)）：

```json
{ "text": "你好，欢迎使用。", "voice": "中文女", "speed": 1.0, "format": "wav", "model": null }
```

- `text`：1–5000 字符（超限 422）。
- `speed`：0.5–2.0（**一次性合成生效**）。
- `format`：仅 `wav`。
- `model`：可选，指定 TTS 引擎名；未知/空回退默认引擎。

响应：`audio/wav` 二进制。QoS 走**响应头**（body 是音频）：`X-Node` / `X-Model` / `X-Process-Ms` / `X-Audio-Ms` / `X-RTF`，并 `Access-Control-Expose-Headers` 放行前端读取。见 [routes_tts.py:38-48](../../backend/app/api/routes_tts.py#L38-L48)。

### 3.2 流式合成 `WS /ws/tts`

上行首帧：`{"type":"synthesize","text":"...","voice":"中文女","speed":1.0,"model":null}`（非 `synthesize` 即报错关闭）。

下行序列（见 [routes_tts.py:71-86](../../backend/app/api/routes_tts.py#L71-L86)）：

1. `meta` 帧：`{"type":"meta","sample_rate":24000,"format":"pcm_s16le","node":"...","model":"..."}`
2. 若干**二进制帧**：裸 16bit 小端 PCM（无 WAV 头），前端 `StreamingPcmPlayer` 边收边播。
3. `done` 帧：`{"type":"done","qos":{...}}`（流耗尽后填入 ttfb/process/audio/rtf）。
4. 关闭。

错误：并发满 → `{"type":"error","code":"busy","retry_after":N}`；其它异常 → `{"type":"error","message":...}`。

### 3.3 模型/音色发现 `GET /api/v1/models`

TTS 段每项：`{name, kind:"tts", expected_sample_rate, languages:[音色名...], default}`。注意**音色列表复用 `languages` 字段**返回（见 [routes_health.py:54-63](../../backend/app/api/routes_health.py#L54-L63)），前端据此填充音色下拉；若线上遗留音色 id 为技术名 `default`，前端展示层映射为「中文女」。需要「中文男」真正是男声时，必须在 `COSYVOICE_VOICES` 里注册独立男声参考音频（如 `/voices/male.wav`）和逐字一致的 `prompt_text`。

---

## 4. 延迟优化（已落地 / 已评估）

TTS 的核心延迟指标是 **TTFB（首包延迟）**：从请求到第一块音频返回。CosyVoice2 的 TTFB 随输入文本长度增长（len=3→~1.6s，len=89→~5s），根因在其 Qwen2-0.5B LLM 的**自回归段**：必须先把整段文本喂进去、生成第一个语音 token 才返回首块。

四轮优化，只有**分句流式**是正面成果：

| 方案 | 结论 | 依据 |
|---|---|---|
| **分句流式** | ✅ **采纳**（node1 开、node2 关作对照） | 把长文本按标点分句，同一 GPU 槽内逐句合成，让首个短句快速出首块 → TTFB 绑定到短输入。零依赖、引擎无关。 |
| **markdown 前处理** | ✅ **采纳**（双机无条件生效） | 不是延迟优化，是**正确性**：去掉 md 语法避免逐字念符号/URL。 |
| **领域 TN / 纯数字兜底** | ✅ **采纳** | `apply_domain_tn` 按前端勾选类别做少数领域预转换；`apply_default_tts_tn` 仅兜底整段纯数字（如 `1234`→「一千二百三十四」），避免无中文上下文时被模型内置 TN 读成英文。 |
| fp16 | ❌ 不采纳 | 实测 5 语料 4 条更慢；瓶颈在自回归串行生成非矩阵算子。 |
| vLLM | ❌ 不采纳 | T4 显存与常驻 ASR/TTS 强冲突 + Turing 不支持 bf16。 |
| TensorRT | ❌ 不采纳 | 只加速 flow.decoder.estimator（命中吞吐非 TTFB），改造成本高。 |

> 详细数据与技术判断见附录指针（§7）。

### 4.1 分句流式（`TTS_SENTENCE_STREAM`）

- 开关：`tts_sentence_stream`（默认 `False`）；单句上限 `tts_max_sentence_chars`（默认 60）；`tts_min_sentence_chars`（默认 0，>0 时贪心合并过短段以消除句间播放间隙）。见 [config.py:112-120](../../backend/app/config.py#L112-L120)。
- 逻辑：`split_sentences` 主标点切句、超长句按次标点/硬截断切到 max_chars 内；只在 `tts_stream` 生效，`tts_file` 不分句（一次性返回无收益）。
- **关键约束**：整个分句循环必须在**同一个 `tts_slot()` 内**，避免逐句重新排队触发 429；`ttfb_ms` 只在**全局首个 chunk** 翻转。见 [dispatcher.py:202-243](../../backend/app/orchestration/dispatcher.py#L202-L243)。

---

## 5. 关键实现细节与约束

### 5.1 单卡 GPU 并发闸（防 OOM / 雪崩）

- TTS 请求经 `limiter.tts_slot()` 获取信号量（默认 `tts_concurrency=2`）；`gpu_acquire_timeout` 内排不到即抛 `ConcurrencyLimitError` → 路由返回 429 + `Retry-After`。见 [limiter.py](../../backend/app/orchestration/limiter.py)。
- ASR / TTS 各持独立信号量，互不挤占；但同一张 T4 上二者共享算力，聊天场景 ASR+TTS+Agent 并发时需注意显存/算力争用。

### 5.2 同步生成器 → 异步桥接

CosyVoice 推理是**同步生成器**，不能直接在事件循环里迭代（会阻塞）。`synthesize_stream` 用 `asyncio.to_thread` 起生产线程跑 `inference_zero_shot(stream=True)`，产出的 PCM 经 `loop.call_soon_threadsafe(queue.put_nowait, ...)` 投递到 `asyncio.Queue`，异步侧从队列取块 yield；异常也经队列透传，`_DONE` 哨兵收尾。见 [cosyvoice_engine.py:119-146](../../backend/app/engines/cosyvoice_engine.py#L119-L146)。

### 5.3 流式变速失效（固有限制）

`synthesize_stream` 内 speed 被硬编码 `1.0`（[cosyvoice_engine.py:130-131](../../backend/app/engines/cosyvoice_engine.py#L130-L131)）——CosyVoice2 流式不做插值变速（分块插值会产生拼接杂音）。因此：

- **一次性合成**（`synthesize`, `stream=False`）：speed 真正传入模型 → **变速生效**。
- **流式合成 / 聊天**（`synthesize_stream`）：speed 被忽略 → **变速无效**。
- 前端已据此处理：独立 TTS 页语速滑块标注「仅一次性合成生效」；语音聊天页**已移除**语速控件（聊天必走流式）。

### 5.4 文本前处理（markdown + 薄 TN）

`strip_markdown` 在送引擎前把 md 转成适合朗读的纯文本：链接留文字丢 URL、图片删除、裸 URL 删除、去强调/代码符号、行首标记转停顿、表格竖线转逗号。`tts_file` 在 `synthesize` 前调用；`tts_stream` 在 `split_sentences` **之前**调用（先剥 md 再分句，两优化正交叠加）。无 env 开关（纯文本经过基本不变）。见 [text_clean.py](../../backend/app/utils/text_clean.py) 与 [dispatcher.py:162](../../backend/app/orchestration/dispatcher.py#L162)、[dispatcher.py:204](../../backend/app/orchestration/dispatcher.py#L204)。

`apply_domain_tn` 挂在 `strip_markdown` 之后、模型内置 TN 之前，仅在前端勾选类别时生效；`apply_default_tts_tn` 无开关但范围极窄，只处理「整段文本就是数字/小数」的中文读法兜底，例如 `1234` 先转为「一千二百三十四」。完整数字/标点/中英混排仍交给 CosyVoice 内置 `text_normalize`，我们不关闭也不替换模型 frontend。

### 5.5 QoS 语义

- `ttfb_ms`：请求到首个 PCM 块（仅流式）。`process_ms`：整段总合成耗时。`audio_ms`：产出音频时长。`rtf` = process_ms / audio_ms（<1 表示快于实时）。
- 一次性合成经响应头下发；流式经 `done` 帧下发；均带 `node` 标签供双机灰度对比，同步写 Prometheus（`tts_ttfb_seconds` / observe_tts）。

---

## 6. 双机部署与当前配置

TTS 引擎在 **node1 + node2** 均部署（`/ws/tts`、`/api/v1/models`、`/ws/chat` 均双机负载均衡，见 [nginx.conf](../../frontend/nginx.conf)）。两机同款 Tesla T4（15360 MiB）、同 CosyVoice2-0.5B、同关闭 fp16/trt/vllm。

TTS 相关配置双机对比（本会话容器内实测）：

| 配置项 | node1（入口机） | node2（扩展机） | 说明 |
|---|---|---|---|
| `tts_engine` | cosyvoice | cosyvoice | 一致 |
| `cosyvoice_fp16 / load_trt / load_vllm` | 均 False | 均 False | 一致（评估后统一关闭） |
| `chat_tts_sentence_stream`（聊天逐句） | True | True | 一致 |
| `tts_sentence_stream`（独立 TTS 页流式） | **True** | **False** | ⚠️ 唯一差异：node1 分句、node2 整段 |
| `tts_min_sentence_chars` | **5** | **0** | node1 合并 <5 字短段 |
| `tts_max_sentence_chars` | 60 | 60 | 一致 |

> 独立 TTS 页的分句差异只影响 `/ws/tts` 端点长文本的首包/句间平滑，对聊天 TTS 无影响（聊天两机都开逐句流式）。

### 6.1 部署要点

- **node1**（Docker Hub 可达）：常规 `docker compose build backend && up -d backend`；前端经 `deploy/sync.sh` 构建 dist + 重建 web。
- **node2**（Docker Hub 不可达，backend-only）：**overlay 增量构建**（`FROM asr-tts-backend:latest` + `COPY app /app/app`，零联网），再 `docker compose up -d --no-build backend`。见《水平扩容Runbook》。
- **配置生效**：改 node-local `.env.deploy` 后需 `--force-recreate`（Compose 不自动感知 env_file 内容变化）。改前备份。API key 只走 node-local `.env.deploy`，不入库。
- ⚠️ **docker cp 会被 recreate 冲掉**：任何 `docker cp` 的临时改动会在下次 `compose up -d`（recreate）时回退到镜像内旧代码，治本必须 build 进镜像。

---

## 7. 附：相关分散记录指针

- **分句流式 TTFB 优化**（完整设计 + 实施 + 压测）：《[分句流式TTS-TTFB优化方案](sentence-streaming.md)》
  - §附「markdown 剥离」（2026-07-01，双机落地）
  - §附「fp16 灰度对照——负面结论」（2026-07-05 数据快照）
- **多句正式压测与文档补全**：《[分句流式-正式多句压测与文档补全](sentence-streaming-loadtest.md)》
- **vLLM / 量化可行性（含 TTS 显存基线）**：《[T4显存限制下vLLM可行性评估报告](../evaluations/t4-vllm.md)》
- **TTS 作为聊天回复播放底层**：《[语音聊天-语音到语音对话方案](../chat/speech-to-speech.md)》
- **可观测性（TTS QoS 监控）**：《[可观测性Runbook-QoS监控栈](../observability/qos-monitoring.md)》
- **主技术方案**：《[语音大模型后端服务-技术方案与实施计划](../architecture/overview.md)》

---

## 8. 验证清单

- 一次性合成：`POST /api/v1/tts` 带 markdown 文本返回 HTTP 200 `audio/wav`，响应头 QoS 完整；变速（speed=0.5/2.0）音频时长随之变化。
- 流式合成：`WS /ws/tts` 收到 `meta` → ≥1 二进制块 → `done`（含 qos）；分句开启时长文本 TTFB 显著低于整段基线。
- 并发：超过 `tts_concurrency` 且排队超时返回 429 / `busy`。
- 双机：`/api/v1/models` 两机均列出 TTS 引擎 + 音色；Prometheus `tts_ttfb_seconds` by node 有数据。
- 本地：`ASR_ENGINE=stub TTS_ENGINE=stub pytest -q` 全绿（text_split / text_clean / dispatcher / api 回归）。
