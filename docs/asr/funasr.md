> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 语音识别（ASR / FunASR）服务技术方案

> 状态：**已落地并上线**（双机 node1/node2）| 创建：2026-07-07 | 技术栈：Python(FastAPI) + React/TS，推理引擎 FunASR（阿里 DAMO）：SenseVoiceSmall + Paraformer-online + SeacoParaformer，单卡 Tesla T4（15GB）
>
> 定位：本服务三大业务能力（**ASR / TTS / 语音聊天**）之一。ASR 提供「语音 → 文本」识别，对外暴露两种调用方式：文件转写（REST，整段上传返回全文）与实时流式（WebSocket，边说边出字）。ASR 同时是语音聊天（speech-to-speech）的上游——用户语音先经流式 ASR 转写再送 Agent（见《语音聊天-语音到语音对话方案》）。
>
> 本文档是 ASR 能力的**顶层方案索引**，串联：文件转写、实时流式 2pass、多模型注册、热词偏置、降级熔断。实时流式的完整设计细节见专题《[实时录音转写-真流式2pass方案](streaming-2pass.md)》。

---

## 1. 能力概述与设计边界

### 1.1 提供什么

| 能力 | 入口 | 返回 | 典型场景 |
|---|---|---|---|
| 文件转写 | `POST /api/v1/asr` | 完整 `ASRResponse`（text + segments + QoS） | 上传音频文件、离线整段识别、需要高精度/热词 |
| 实时流式 | `WS /ws/asr` | `partial`（临时字，灰）→ `final`（定稿，黑）逐句下发 | 边说边转写、实时字幕、低延迟出字 |
| 聊天上游转写 | `WS /ws/chat`（复用 `dispatcher.asr_stream`） | 同流式 | 语音聊天里把用户语音转成文本送 Agent |

### 1.2 模型选型（多引擎并存）

| 引擎 name | 底层模型 | flavor | 定位 | 热词 |
|---|---|---|---|---|
| `funasr-sensevoice` | SenseVoiceSmall | sensevoice | 多语种、低延迟、自带富文本（情感/事件标签） | ❌ |
| `funasr-paraformer-zh` | Paraformer-zh | paraformer | 中文高精度 + 时间戳 + 标点（可选，配 model id 才注册） | ❌ |
| `funasr-seaco` | SeacoParaformer | paraformer | **唯一支持热词偏置**（bias encoder），挂标点模型 | ✅ |
| `funasr-streaming` | Paraformer-online + 上述做 2pass 定稿 | 流式 | **实时流式 2pass**（线上默认引擎） | ✅（经 seaco 定稿）|

- **模型加载策略**：`AutoModel` 懒加载（首次调用/warmup 时才 load），各引擎独立实例。同步阻塞推理统一用 `asyncio.to_thread` 包裹，避免阻塞事件循环。见 [funasr_engine.py:60-83](../../backend/app/engines/funasr_engine.py#L60-L83)。
- **输入契约**：16000 Hz 单声道 float32 PCM（`expected_sample_rate=16000`）；非目标采样率/声道由 L3 预处理层自动重采样。

### 1.3 明确不做（范围裁剪）

- ❌ 流式第一遍热词：paraformer-online 架构限制，滚动临时字始终无热词，句末定稿才用 seaco 重解码补偿。
- ❌ 说话人分离 / 声纹：不在范围。
- ❌ 流式引擎的降级熔断：降级链只保护**文件式**（流式是长连接持有单引擎，语义不同，见 §5.2）。

---

## 2. 整体链路

### 2.1 分层结构

```
┌── 客户端 ───────────────────────────────────────────────────┐
│  AsrPage(独立页): 文件上传 / 实时录音                          │
│  ChatPage(语音聊天): 麦克风流 → 复用 asr_stream               │
└──────────────┬───────────────────────┬──────────────────────┘
   POST /api/v1/asr              WS /ws/asr  (WS /ws/chat 内部复用)
               │                           │
┌── L1 路由 (routes_asr.py) ──────────────────────────────────┐
│  asr_file: 校验大小 → 解析 hotwords → dispatcher.asr_file    │
│  asr_stream: 收 start 帧 → PCM 块迭代 → dispatcher.asr_stream│
│             → partial/final 帧 (带 segment_id + node)        │
└──────────────┬──────────────────────────────────────────────┘
               │
┌── L2 编排 (dispatcher.py) ──────────────────────────────────┐
│  asr_file:  registry.asr_chain(model) → 降级链 + 熔断        │
│             preprocess_file → limiter.asr_slot → transcribe  │
│             → QoS(process/rtf/degraded) + observe_asr        │
│  asr_stream: registry.asr(model) → 单引擎                    │
│             preprocess_pcm(逐块重采样) → asr_slot(持整条流)   │
│             → transcribe_stream 透传 partial/final           │
└──────────────┬──────────────────────────────────────────────┘
               │
┌── L3 引擎 (funasr_engine.py) ───────────────────────────────┐
│  FunASREngine: 文件式整段 generate + SenseVoice 富文本后处理  │
│  FunASRStreamingEngine: 2pass(paraformer-online + 定稿重解码) │
│  同步 generate 用 asyncio.to_thread 桥接                     │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 关键文件

| 层 | 文件 | 职责 |
|---|---|---|
| L1 路由 | [routes_asr.py](../../backend/app/api/routes_asr.py) | REST + WS 两端点、start 帧解析、WS 帧契约 |
| L1 模型发现 | [routes_health.py:42-52](../../backend/app/api/routes_health.py#L42-L52) | `/api/v1/models` 暴露 ASR 引擎名/语言/是否支持热词 |
| L2 编排 | [dispatcher.py:64-155](../../backend/app/orchestration/dispatcher.py#L64-L155) | `asr_file`（降级链）/ `asr_stream`（单引擎） |
| L2 降级链 | [registry.py:103-113](../../backend/app/engines/registry.py#L103-L113) | `asr_chain`：首选 + 其余兜底 |
| L2 熔断 | [breaker.py](../../backend/app/orchestration/breaker.py) | 连续失败打开、cooldown 短路、半开试探 |
| L2 并发闸 | [limiter.py:67-68](../../backend/app/orchestration/limiter.py#L67-L68) | `asr_slot()` 单卡 GPU 信号量 |
| L3 引擎注册 | [registry.py:28-79](../../backend/app/engines/registry.py#L28-L79) | 多模型按 env 条件注册 |
| L3 离线引擎 | [funasr_engine.py:26-163](../../backend/app/engines/funasr_engine.py#L26-L163) | `FunASREngine`：文件式 + 事件标签过滤 |
| L3 流式引擎 | [funasr_engine.py:166-347](../../backend/app/engines/funasr_engine.py#L166-L347) | `FunASRStreamingEngine`：2pass |
| L3 引擎契约 | [base.py:23-58](../../backend/app/engines/base.py#L23-L58) | `ASREngine.transcribe / transcribe_stream` |
| 契约 | [models.py:6-23](../../backend/app/schemas/models.py#L6-L23) | `ASRResponse` / `ASRSegment` |

---

## 3. 接口契约

### 3.1 文件转写 `POST /api/v1/asr`

- 表单字段：`file`（音频，≤`max_upload_bytes`，超限 413；空 400）、`language`（默认 auto）、`hotwords`（逗号分隔字符串，可选）、`model`（可选，指定引擎名）。
- 返回 `ASRResponse`：`text` / `segments[{text,start_ms,end_ms}]` / `audio_duration_ms` / `process_ms` / `rtf` / `model`（实际使用引擎）/ `degraded`（是否降级）/ `node`。见 [routes_asr.py:25-52](../../backend/app/api/routes_asr.py#L25-L52)。
- 过载：并发满且排队超时 → 429 + `Retry-After`。

### 3.2 实时流式 `WS /ws/asr`

上行首帧 start：`{"type":"start","sample_rate":16000,"channels":1,"language":"auto","model":null,"hotwords":"词1,词2"}`；随后二进制 float32 PCM 块（前端 ~85ms/块）；`{"type":"end"}` 结束。

下行（见 [routes_asr.py:95-107](../../backend/app/api/routes_asr.py#L95-L107)）：
- `{"type":"partial","text":"当前句临时字","segment_id":N,"node":"..."}` —— 灰字，随聚合窗滚动。
- `{"type":"final","text":"当前句定稿","segment_id":N,"node":"..."}` —— 黑字，句末 2pass 重解码覆盖临时字。
- 错误：`{"type":"error","code":"busy",...}` / `{"type":"error","message":...}`。

> **协议要点**：`ASRPartial.text` 承载「当前这一句」（非累计全文），`segment_id` 为句序号；前端据 `segment_id` 做「灰字→黑字」回改渲染。

### 3.3 模型发现 `GET /api/v1/models`

ASR 段每项：`{name, kind:"asr", expected_sample_rate, languages:[...], default, supports_hotwords}`。前端据 `supports_hotwords` 启用/禁用热词输入框。见 [routes_health.py:42-52](../../backend/app/api/routes_health.py#L42-L52)。

---

## 4. 实时流式 2pass（核心机制）

`FunASRStreamingEngine` 用「两遍解码」平衡延迟与准确率：

- **第一遍（低延迟出字）**：`paraformer-zh-streaming` 真流式模型，按**聚合窗**（`funasr_streaming_chunk_ms`）持续增量出字，每个聚合块产出当前句临时文本（`is_final=False`）。
- **第二遍（字符修正）**：流式 `fsmn-vad` 检测句子端点；句末把该句原始音频交给**已加载的 offline 引擎**整句重解码，产出定稿（`is_final=True`）覆盖临时字。
- **定稿引擎动态选择**（零额外显存，两引擎均常驻）：不带热词 → `offline_engine`（SenseVoice，保多语种/富文本）；带热词 → `hotword_engine`（SeacoParaformer，唯一支持热词偏置）；未配 seaco 时回退 SenseVoice、热词被忽略。见 [funasr_engine.py:245-256](../../backend/app/engines/funasr_engine.py#L245-L256)。

### 4.1 聚合窗约束（重要）

- `funasr_streaming_chunk_ms` 与 `funasr_streaming_chunk_size[1]` **强耦合**：paraformer 每帧 60ms，600ms=`[0,10,5]`，480ms=`[0,8,4]`。400ms÷60 不整除故取 480ms。**改一个必须同步改另一个**。
- 实测：600→480ms 首字延迟 ↓25.4%，字准不降反升，双机推全（详见专题文档）。

> 完整实现（前端双区渲染、VAD 端点、字符修正回退、灰度纪律）见《[实时录音转写-真流式2pass方案](streaming-2pass.md)》。

---

## 5. 多模型、热词、降级熔断

### 5.1 多模型注册（按 env 条件）

`EngineRegistry._build_asr`（[registry.py:28-79](../../backend/app/engines/registry.py#L28-L79)）按配置条件注册：
- `funasr-sensevoice`：`asr_engine=funasr` 时**必注册**（基底）。
- `funasr-paraformer-zh`：配 `funasr_paraformer_model` 才注册。
- `funasr-seaco`：配 `funasr_seaco_model` 才注册（热词引擎）。
- `funasr-streaming`：配 `funasr_streaming_model` 才注册；复用 sensevoice 作 offline 定稿、seaco 作热词定稿（若已注册）。
- 默认引擎 = `default_asr_model`（留空则第一个注册的）。

### 5.2 热词偏置

- **只有 SeacoParaformer（`funasr-seaco`）真正支持热词**（bias encoder）。其它引擎的 `supports_hotwords=False`，前端据此禁用热词框。
- 文件式：直接透传 `hotword`（空格拼接）给 seaco。见 [funasr_engine.py:109-110](../../backend/app/engines/funasr_engine.py#L109-L110)。
- 流式：第一遍无热词（架构限制），**句末定稿**改用 seaco 重解码时才生效；`funasr-streaming.supports_hotwords` 仅当注入了 seaco 时才为 True。

### 5.3 降级链 + 熔断（仅文件式）

- **降级链**：`asr_chain(model)` = 首选引擎 + 其余已注册引擎兜底（[registry.py:103-113](../../backend/app/engines/registry.py#L103-L113)）。主引擎超时/OOM/异常 → 记失败、尝试下一个；响应 `degraded=True` 标注降级。见 [dispatcher.py:64-135](../../backend/app/orchestration/dispatcher.py#L64-L135)。
- **熔断**：`CircuitBreaker` 按引擎名跟踪连续失败，达 `breaker_fail_threshold`（默认 3）即打开，`breaker_cooldown`（默认 30s）内短路跳过，冷却后半开试探（[breaker.py](../../backend/app/orchestration/breaker.py)）。
- **限流不计熔断**：`ConcurrencyLimitError` 是过载信号非引擎故障，直接上抛转 429，不记失败。
- ⚠️ **流式不走降级熔断**：`asr_stream` 用单引擎持整条长连接（[dispatcher.py:137-155](../../backend/app/orchestration/dispatcher.py#L137-L155)），中途换引擎无意义，故不套降级链。

### 5.4 非语音事件过滤（咳嗽等）

SenseVoice 富文本含 `<|Cough|>`/`<|Sneeze|>`/`<|BGM|>` 等事件标签。`_is_nonspeech_event` 在 `_postprocess` **之前**拦截：命中白名单且不含 `<|Speech|>` → 判噪声、丢弃整段（[funasr_engine.py:130-140](../../backend/app/engines/funasr_engine.py#L130-L140)）。开关 `funasr_drop_nonspeech_events`（默认 True）。这是语音聊天「三层噪声防护」的一层（详见语音聊天方案 §14）。

---

## 6. 关键约束与已知坑

1. **聚合窗耦合**：`chunk_ms` 改动必须同步改 `chunk_size`（见 §4.1），否则模型帧配置错位。
2. **同步 generate → 异步桥接**：FunASR 推理同步阻塞，全部 `asyncio.to_thread` 包裹；流式引擎 `_flush` 也在线程内跑。
3. **单卡 GPU 并发闸**：ASR 走 `asr_slot()`（默认 `asr_concurrency=2`），流式**整条流持有一个许可**直到连接结束——高并发实时流会较快占满槽位。
4. **流式第一遍无热词**：只能靠句末定稿补偿；对「首屏临时字要热词」的诉求无解（架构限制）。
5. **SenseVoice 无置信度**：只输出 key/timestamp，无 score，故噪声判据只能用事件标签而非置信度。

---

## 7. 双机部署与当前配置

ASR 引擎在 **node1 + node2** 均部署（`/ws/asr`、`/api/v1/models`、`/ws/chat` 双机负载均衡，见 [nginx.conf](../../frontend/nginx.conf)）。两机同款 Tesla T4、同引擎栈。

node1 实测 ASR 配置（本会话容器内 + `/api/v1/models`）：

| 配置项 | node1 值 | 说明 |
|---|---|---|
| `asr_engine` | funasr | |
| `funasr_model` | SenseVoiceSmall | 基底 offline |
| `funasr_seaco_model` | seaco_paraformer... | 热词引擎已注册 |
| `funasr_streaming_model` | paraformer...-online | 流式引擎已注册 |
| `funasr_paraformer_model` | （空） | 未注册独立 paraformer-zh |
| `default_asr_model` | **funasr-streaming** | 默认走实时流式 2pass |
| `funasr_streaming_chunk_ms` | **480**（node2=600） | ⚠️ 双机差异：node1 优化版，node2 基线对照 |
| `funasr_streaming_chunk_size` | `[0,8,4]`（node2=`[0,10,5]`） | 与 chunk_ms 联动 |
| `funasr_drop_nonspeech_events` | True | 咳嗽等事件过滤 |
| `funasr_stream_correct_enabled` | True | 2pass 字符修正 |
| `asr_concurrency` | 2 | |
| `asr_fallback_enabled` | True | 文件式降级链 |
| `breaker_fail_threshold` / `cooldown` | 3 / 30s | 熔断 |

**注册的 ASR 模型**（node1）：`funasr-sensevoice`（hotwords=False）、`funasr-seaco`（hotwords=True）、`funasr-streaming`（default=True, hotwords=True）。

> 双机唯一 ASR 差异：**聚合窗**（node1=480ms 优化 / node2=600ms 基线），保留灰度对照。其余引擎栈、热词、降级熔断配置一致。

### 7.1 部署要点

- **node1**（Docker Hub 可达）：常规 `docker compose build backend && up -d backend`。
- **node2**（Docker Hub 不可达，backend-only）：overlay 增量构建（`FROM asr-tts-backend:latest` + `COPY app`，零联网）。见《水平扩容Runbook》。
- **配置生效**：改 node-local `.env.deploy` 后需 `--force-recreate`；改前备份；聚合窗两参数成对改。
- ⚠️ docker cp 会被 recreate 冲掉，治本必须 build 进镜像。

---

## 8. 相关文档

- 实时流式 2pass 完整设计 + 480ms 聚合窗实测：《[实时录音转写-真流式2pass方案](streaming-2pass.md)》
- ASR 作为聊天上游 + 三层噪声门控 + 咳嗽事件层：《[语音聊天-语音到语音对话方案](../chat/speech-to-speech.md)》
- 姊妹能力顶层方案：《[语音合成TTS-CosyVoice2方案](../tts/cosyvoice2.md)》
- 可观测性（ASR QoS 监控）：《[可观测性Runbook-QoS监控栈](../observability/qos-monitoring.md)》
- 主技术方案：《[语音大模型后端服务-技术方案与实施计划](../architecture/overview.md)》

---

## 9. 验证清单

- 文件转写：`POST /api/v1/asr` 返回 `ASRResponse`，`model`/`degraded`/`node` 字段完整；带 `hotwords` 且指定 `funasr-seaco` 时热词生效。
- 实时流式：`WS /ws/asr` 收 start → 多个 `partial`（同一 segment_id 滚动）→ 句末 `final` 覆盖；`segment_id` 递增。
- 降级：主引擎故障时响应 `degraded=True` 且 `model` 为兜底引擎；连续失败达阈值后该引擎被熔断短路。
- 并发：超过 `asr_concurrency` 且排队超时返回 429 / `busy`。
- 双机：`/api/v1/models` 两机均列出 3 个 ASR 引擎；聚合窗 node1=480 / node2=600。
- 本地：`ASR_ENGINE=stub pytest -q` 全绿。
