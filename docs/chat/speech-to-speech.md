> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 语音聊天（语音到语音对话）服务技术方案

> 状态：**已落地并上线**（v1 设计稿 → 全部实施；barge-in 已于 2026-07-04 作为可开关增强上线，见 §12）| 创建：2026-06-28 | 更新：2026-07-04 | 技术栈：复用现有 Python(FastAPI) + React/TS 后端 + 远端 AI Agent（OpenAI 兼容流式）
>
> 定位：在既有 **ASR / TTS** 两项能力之外，新增第三项业务能力 **语音聊天**（speech-to-speech）——用户对着麦克风说话，后端实时转写 → 转发给一个**远端 AI Agent**（LLM，OpenAI 兼容 `/v1/chat/completions` SSE 流式）→ 把 Agent 逐 token 回复**边到边合成语音**回放，形成低延迟的日常语音对话。
>
> 本能力属于主技术方案《语音大模型后端服务-技术方案与实施计划》Phase 3 的能力扩展，不改动 ASR/TTS 既有链路。

---

## 1. 需求与已确认决策

用户原始需求：「除了 ASR 和 TTS 以外，我还想要增加一个服务，可以流式地与一个远端的 AI Agent 做日常的语音聊天，帮忙设计一个技术方案。」

经 AskUserQuestion 确认的 4 项关键决策（构成 v1 的设计边界）：

| # | 决策点 | 选择 | 含义 |
|---|---|---|---|
| ① | **远端 Agent 协议** | OpenAI 兼容流式 | 后端用 `POST {agent_endpoint}/v1/chat/completions`，`stream=true`，解析 SSE `data:` 增量 `delta.content`。兼容 vLLM / DashScope(兼容模式) / 火山方舟 / OpenAI 等几乎所有主流推理服务，换 Agent 只改 env。 |
| ② | **用户交互方式** | 自动 VAD 免提 | 麦克风持续录音，后端复用流式 ASR 的 fsmn-vad 端点检测；检测到「说完一句」自动把整句交给 Agent，用户无需手动点按。（**注：初版仅用 VAD 做「句末端点」，不做噪声过滤，导致环境噪声误触发；2026-07-04 已补前端能量 VAD 门控 + 后端有效发言门控，见 §13；2026-07-05 再补 SenseVoice 事件标签层拦咳嗽等非语言声，见 §14。**） |
| ③ | **barge-in 打断** | v1 先不做（**已于 2026-07-04 落地**，见 §12） | v1 初版：Agent 说话时用户插话不打断，简化状态机与回声处理。**后续增强**：加浏览器端 AEC + 可开关的半双工打断，`chat_barge_in` 默认关；开启后 RESPONDING 期间跑 ASR 检测插话即中止当前回复。 |
| ④ | **多轮记忆** | 保留多轮上下文 | 后端按 WS 连接维持 `system + 历史 messages`，带窗口截断（按轮数 / token 预算）。一次连接即一段完整对话。 |

### v1 明确不做（范围裁剪）

- ❌ barge-in（说话打断 Agent）——决策③。
- ❌ 跨连接的对话持久化 / 会话存储——一次连接一段对话，断开即清空（v2 可加 OSS/Redis 持久化）。
- ❌ Agent 的工具调用（function calling）链路展示——Agent 内部可调工具，但本服务只消费最终文本流。
- ❌ 情绪化控制 / 自动动态换音色——聊天回复音色由前端「高级设置」在连接开始前选择，连接内固定。

---

## 2. 整体链路与对话环

### 2.1 端到端数据流

```
┌── 浏览器 ChatPage ──────────────────────────────────────────┐
│  麦克风 → MicRecorder(16k float32 PCM 块, 复用现有 recorder.ts) │
│        ↘ 二进制帧 ↗            ↖ int16 PCM 帧 + 控制 JSON       │
└───────────────── wss /ws/chat ──────────────────────────────┘
                          │
┌── 后端 node (L1 routes_chat) ───────────────────────────────┐
│  ConversationOrchestrator (L2) —— 单连接的对话状态机          │
│   1) PCM 块 → asr_stream (复用 FunASRStreamingEngine 2pass)   │
│        partial 逐字下行(给前端显示「我在说…」)                  │
│        final(句末, VAD 端点触发) = 用户这一句定稿              │
│   2) 把 final 追加进 messages, 调 AgentClient (L4)            │
│        → 远端 /v1/chat/completions stream=true (SSE)         │
│        ← 逐 token delta.content                              │
│   3) token 累积 → split_sentences 聚合成「可合成句」            │
│        每凑满一句 → dispatcher.tts_stream(句)                 │
│        → int16 PCM 块边合成边下行 → 前端 StreamingPcmPlayer 播放│
│   4) Agent 整段结束 → 把完整回复追加进 messages(多轮记忆)       │
│        → 回到 LISTENING                                      │
└─────────────────────────────────────────────────────────────┘
                          │ (出网)
              远端 AI Agent (OpenAI 兼容 LLM 服务)
```

### 2.2 turn 状态机（单连接内循环）

```
        ┌──────────── (连接建立, 发 ready) ─────────────┐
        ▼                                               │
   ┌─────────┐  VAD 句末(final)   ┌──────────┐  首 token  ┌────────────┐
   │LISTENING│ ───────────────▶  │ THINKING │ ─────────▶ │ RESPONDING │
   │(持续录音)│   用户整句定稿     │(等 Agent) │           │(TTS 播放中) │
   └─────────┘                   └──────────┘            └────────────┘
        ▲                              │ Agent 报错/空           │
        │                              ▼                         │
        │                         (error 帧)                     │
        └──────────────── Agent 流结束 + TTS 播放完 ◀────────────┘
```

- **LISTENING**：持续吃 PCM，跑流式 ASR；partial 下行供前端显示用户正在说的内容。
- 收到 ASR `final`（VAD 端点）→ 该句即「用户这一轮」，进入 **THINKING**。
- **THINKING**：把用户句追加进 `messages`，发起 Agent 流式请求，等待首 token。
- 首 token 到达 → **RESPONDING**：边收 token 边按句合成、边播放。
- v1 不做 barge-in：RESPONDING 期间继续接收 PCM 但**丢弃**（不喂 ASR），避免把 Agent 的回声 / 用户插话误当新输入。（**注：barge-in 已落地，开启 `chat_barge_in` 后 RESPONDING 期间 PCM 改喂检测 ASR；见 §12。以下为默认关闭时的行为。**）
- Agent 流结束且 TTS 全部下发完 → 把完整回复写回 `messages`，回到 **LISTENING**。

> **关键设计点（v1 简化）**：由于不做 barge-in，且前端外放会被麦克风收到（回声），RESPONDING 阶段后端**暂停消费 ASR**。前端也在 RESPONDING 期间可选择性静音麦克风采集（双保险）。这是 v1 单 GPU + 无 AEC 条件下最稳的做法。

---

## 3. 后端设计（最小新增，最大复用）

新增 **3 个组件**，全部复用既有 Dispatcher / 引擎 / 限流 / 熔断，不碰 ASR/TTS 既有路由。

### 3.1 L4 — `AgentClient`（新增 `app/engines/agent_client.py`）

职责：封装「OpenAI 兼容流式 chat completion」的出网调用，吐出增量 token。

契约（仿 `base.py` 的引擎风格）：

```python
class AgentClient:
    def __init__(self, settings: Settings): ...
    async def stream_chat(
        self, messages: list[dict], *, request_id: str
    ) -> AsyncIterator[str]:
        """POST {endpoint}/v1/chat/completions, stream=true。
        逐条解析 SSE: 行首 'data: ' → json → choices[0].delta.content。
        收到 'data: [DONE]' 结束。yield 非空 content 增量。
        """
    async def healthcheck(self) -> bool: ...
```

实现要点：
- HTTP 客户端用 **httpx.AsyncClient**（已是 FastAPI 生态常用；若未引入则加入依赖，**装包前先告知确认**）。
- 请求体：`{"model": agent_model, "messages": [...], "stream": true, "temperature": ..., "max_tokens": ...}`。
- 鉴权头：`Authorization: Bearer {agent_api_key}`（key 走 node-local `.env.deploy`，**不入库**）。
- SSE 解析：按行读 `aiter_lines()`，剥 `data: ` 前缀，跳过 `[DONE]` 与心跳空行，`json.loads` 后取 `delta.content`（缺字段安全跳过）。
- 超时：连接超时 + 首 token 超时 + 总超时（`agent_timeout`），超时抛异常由 Orchestrator 转 error 帧。
- 失败语义：网络/5xx/超时 → 抛异常；Orchestrator 下发 `{type:error}` 并回到 LISTENING，不污染 `messages`。

> 注意：AgentClient 是**出网 I/O**，不占 GPU，**不进 GpuLimiter**。但建议给它独立的并发上限（`agent_concurrency`）防止远端被打爆。

### 3.2 L2 — `ConversationOrchestrator`（新增 `app/orchestration/conversation.py`）

职责：单条 `/ws/chat` 连接的对话状态机与三段编排（ASR → Agent → TTS）。持有：

- `dispatcher`（复用，用其 `asr_stream` / `tts_stream`）；
- `agent_client`（复用 app.state 单例）；
- 本连接的 `messages`（`system` + 多轮 user/assistant），带窗口截断。

核心方法：

```python
class ConversationOrchestrator:
    def __init__(self, dispatcher, agent_client, settings): ...

    async def run(self, ws: WebSocket) -> None:
        """主循环: 解析首帧 config → 循环 turn:
           listen() → think_and_respond() → 写回 messages → 继续。"""

    async def _listen(self, pcm_iter) -> str:
        """喂 dispatcher.asr_stream, 下行 user_partial/user_final;
           收到第一个 final 即返回用户整句 (VAD 端点)。"""

    async def _think_and_respond(self, ws, user_text) -> str:
        """messages.append(user); 调 agent_client.stream_chat;
           token 累积 → split_sentences(增量) → 每满一句 tts_stream → 下行音频;
           下行 assistant_partial(文本) 供前端字幕; 返回完整 assistant 文本。"""
```

**多轮记忆与窗口截断**：
- `messages` 初始 `[{"role":"system","content": agent_system_prompt}]`。
- 每轮：append user → 流式得到 assistant → append assistant。
- 截断策略：保留 `system` + 最近 `agent_max_turns` 轮（user+assistant 成对）。可叠加 token 预算估算（粗略按字符数）兜底，避免远端 400（超 context）。

**token → 句子的增量聚合（复用 `split_sentences`）**：
- 维护 `text_buffer`，每来一段 delta 追加。
- 调 `split_sentences(text_buffer, max, min)`，若产出 >=2 段则前面的「完整句」立即送 TTS，最后一段（未完句）留在 buffer。
- Agent 流结束时，buffer 残余整体送 TTS（末句）。
- 这与现有分句流式 TTS（`tts_sentence_stream`）思路一致，可直接复用同一断句器，保证首句快出、句间不卡顿。

**GPU 串行约束**：ASR 与 TTS 共享单 T4，且都过 `GpuLimiter`（asr=2 / tts=2 槽）。一个 turn 内 ASR 先完成（LISTENING 结束）才进入 TTS（RESPONDING），天然错峰，不会同 turn 内 ASR+TTS 抢槽。多连接并发时由 limiter 排队 / 429 兜底（见 §6）。

### 3.3 L1 — `routes_chat.py`（新增 `/ws/chat`）+ 装配

- 新增 `app/api/routes_chat.py`，`@router.websocket("/ws/chat")`，`accept` 后构造 `ConversationOrchestrator(...).run(ws)`。
- `main.py`：
  - lifespan 中构造 `AgentClient` 单例挂 `app.state.agent_client`（带 `agent_endpoint` 配置时才构造；未配则 `/ws/chat` 直接回 error，不影响 ASR/TTS）；
  - `create_app()` 增 `app.include_router(routes_chat.router)`。
- 健康/就绪：`/readyz` 维持只看引擎就绪（Agent 是外部依赖，不纳入 readyz，避免远端抖动拖垮整机就绪）。可在 `/api/v1/models` 旁加一个 `/api/v1/chat/health` 暴露 Agent 可达性（可选）。

### 3.4 配置新增（`app/config.py` 的 `Settings`）

```python
# ---- 语音聊天 (Phase 3 能力扩展) ----
chat_enabled: bool = False          # 总开关; False 时 /ws/chat 返回 unavailable
agent_endpoint: str = ""            # 远端 OpenAI 兼容 base, 如 https://xxx/v1 或 https://xxx
agent_api_key: str = ""             # 走 .env.deploy, 不入库
agent_model: str = ""               # 远端模型名, 如 qwen-plus / ep-xxx
agent_system_prompt: str = "你是一个友好的语音助手，用简短口语化的中文回答。"
agent_max_turns: int = 8            # 多轮窗口: 保留最近 N 轮
agent_temperature: float = 0.7
agent_max_tokens: int = 1024
agent_timeout: float = 30.0         # 总超时(秒)
agent_first_token_timeout: float = 10.0
agent_concurrency: int = 4          # 出网并发上限(非 GPU)
```

> `agent_system_prompt` 提示「简短口语化」很重要：语音场景里长篇大论的回复会让 TTS 播很久、体验差，且占 GPU。

---

## 4. `/ws/chat` 协议

### 4.1 上行（前端 → 后端）

| 帧 | 形态 | 字段 | 说明 |
|---|---|---|---|
| 首帧 config | JSON | `{type:"start", sample_rate:16000, channels:1, language:"auto"}` | 仿 `/ws/asr` start；可选 `system_prompt` 覆盖默认人设、`voice`/`speed` 指定回复音色、`barge_in`(bool) 逐连接开/关说话打断（覆盖 env 默认，见 §12）。 |
| 音频块 | 二进制 | float32 PCM（16k 单声道） | 复用 `MicRecorder`，与 `/ws/asr` 完全一致。 |
| 结束 | JSON | `{type:"end"}` | 用户主动结束整段对话，后端收尾关闭。 |

### 4.2 下行（后端 → 前端）

| 帧 | 形态 | 字段 | 说明 |
|---|---|---|---|
| ready | JSON | `{type:"ready", node}` | 连接就绪，可以说话。 |
| 状态 | JSON | `{type:"state", state:"listening\|thinking\|responding", node}` | 驱动前端 UI（麦克风灯 / 思考中 / 播放中）。 |
| 用户字幕(临时) | JSON | `{type:"user_partial", text, node}` | ASR partial，灰字显示用户正在说的内容。 |
| 用户字幕(定稿) | JSON | `{type:"user_final", text, node}` | ASR final（VAD 端点），用户这一轮定稿。 |
| 助手字幕 | JSON | `{type:"assistant_partial", text, node}` | Agent 增量文本（累计或增量，前端拼接显示）。 |
| 音频元信息 | JSON | `{type:"tts_meta", sample_rate:24000, format:"pcm_s16le", node, model}` | 每一轮回复开始时下发一次（同 `/ws/tts` meta）。 |
| 音频块 | 二进制 | int16 PCM（24k） | TTS 流，复用 `StreamingPcmPlayer` 播放。 |
| 助手回合结束 | JSON | `{type:"assistant_done", text, qos}` | 本轮回复完整文本 + QoS（asr_ms / first_token_ms / tts_ttfb_ms / total_ms）。 |
| 繁忙 | JSON | `{type:"error", code:"busy", retry_after}` | GpuLimiter / agent_concurrency 超限。 |
| 错误 | JSON | `{type:"error", message}` | Agent 出网失败 / 超时等；前端提示后回到 listening。 |

> 设计上**与 `/ws/asr` + `/ws/tts` 的帧语义保持一致**（type/二进制 PCM/meta/done/error/node），前端可大量复用现有解析逻辑，降低实现与认知成本。

---

## 5. 前端设计（新增第三个 tab：ChatPage）

- `App.tsx`：`tab` 类型扩为 `"asr" | "tts" | "chat"`，加「语音聊天」tab，渲染 `<ChatPage />`。
- 新增 `frontend/src/pages/ChatPage.tsx`：
  - 复用 `MicRecorder`（采集 16k float32 PCM）与 `StreamingPcmPlayer`（播放 24k int16 PCM），二者**零改动**。
  - UI：一个「开始对话 / 结束对话」按钮 + 对话气泡列表（用户 / 助手交替）+ 状态指示（聆听 / 思考 / 回答）。
  - 用户气泡：`user_partial` 灰字 → `user_final` 转黑定稿。
  - 助手气泡：`assistant_partial` 流式追加 → `assistant_done` 定稿，气泡下挂 QoS badge（复用 `QosBadge`）。
  - RESPONDING 期间前端暂停 `MicRecorder` 采集（或不发送 PCM），与后端「暂停消费 ASR」呼应，规避回声。
- 新增 `openChatStream`（`client.ts`，仿 `openAsrStream`/`openTtsStream`）：
  ```ts
  export function openChatStream(opts: {
    sampleRate: number; language: string; voice?: string; speed?: number;
    bargeIn?: boolean; model?: string; enableThinking?: boolean;
    onState, onUserPartial, onUserFinal, onAssistantPartial,
    onTtsMeta, onAudio(pcm:Int16Array), onAssistantDone, onError,
  }): WebSocket
  ```
  - `onopen` 发 `{type:"start", ...}`；
  - `onmessage`：string→按 type 分发；ArrayBuffer→`onAudio(new Int16Array(data))`。

---

## 6. 时延预算与单 T4 并发策略

### 6.1 一轮对话时延预算（口语短句场景，目标「自然」）

| 段 | 来源 | 预算 | 备注 |
|---|---|---|---|
| 用户说完 → ASR final | 流式 2pass + VAD 端点 | ~0.3–0.8s | VAD 静音判定窗 + 末句 offline 修正；端点检测本身有固有延迟。 |
| ASR final → Agent 首 token | 远端 LLM prefill + 出网 RTT | ~0.5–2s | **最大不确定项**，取决于远端 Agent 与网络；首 token 超时 `agent_first_token_timeout`。 |
| 首 token → 第一句凑齐 | `split_sentences` 攒到首个句末标点 | ~0.2–0.6s | 短句快出；可调小 `max_sentence_chars` 让首句更短更快。 |
| 首句 → TTS 首块出声 | CosyVoice2 流式 TTFB | ~0.15–0.4s | 既有实测首包 ~150ms 量级。 |
| **用户停 → 听到回复首声** | 累加 | **~1.2–3.8s** | 主要被远端 Agent 首 token 主导。 |

优化手段（v1 可选开启）：
- TTS 开 `tts_sentence_stream=true` + 合理 `min/max_sentence_chars`，让首句尽快出声、句间不断顿。
- `agent_system_prompt` 约束「简短口语化」，缩短回复长度。
- Agent 侧若可控，开启更激进的流式 / 更小的 `max_tokens`。

### 6.2 单 T4 并发（GPU 是瓶颈）

- 既有 `GpuLimiter`：asr=2 / tts=2 槽。语音聊天**完全复用**这套限流——一个 turn 内 ASR 与 TTS 错峰（先听后说），不会同时抢同一类槽。
- 多个聊天连接 + 同时有人用独立 ASR/TTS 页面 → 共享同一 limiter，超限的请求排队 / 超时 429（聊天侧下发 `error code:busy`）。
- 现实容量（单 T4，经验值）：**1–2 路并发语音对话**较稳；更多并发靠双机 LB 横向扩（见 §7）+ 后续 vLLM 化 TTS 提吞吐。
- Agent 出网不占 GPU，但 THINKING 阶段会「占着一个连接等待」，所以 `agent_concurrency` 独立限制，避免大量连接同时挂在远端。

---

## 7. 部署与 node2 出网风险（**落地前必须确认**）

> **【2026-07-04 已更正 —— 本节旧假设被实测推翻】**：node2（<NODE_PUBLIC_HOST>）**实测能端到端连上远端 Agent**。在 node2 容器内直连 Agent 端点 `<OPENAI_COMPATIBLE_AGENT_HOST>`：TLS 握手 85ms、用已配置的 `qwen-plus` 真实调用返回正常。当初「node2 可能无出网」的担心来自早期下模型时 **Docker Hub 不可达**的经验，但阿里云 bailian/maas 端点与 Docker Hub 是两回事——node2 一直能访问阿里云服务（ModelScope/PyPI 也可达）。
>
> **因此聊天已是双机 LB，node1/node2 都能用**：线上 nginx `/ws/chat` 早已指向 `backend_chat`（node1 + node2:8000）双机 upstream。经公网 LB 实测 8 轮，node1/node2 交替各 4 轮、**每轮对话都完整跑通**（ASR→qwen-plus→TTS，`assistant_done` 无错）。node1→node2 内网 8000 链路 3ms 可达。**无需做任何改动，方案 A「钉 node1」已不适用**。以下原文保留为设计期的风险评估记录。

---

**（以下为设计稿原始记录，结论已被上方更正）** 这曾被视为本方案最大的部署风险点：

- 现状：node1（入口机 / 内网 <NODE1_PRIVATE_IP>）有公网；node2（扩展机 / 内网 <NODE2_PRIVATE_IP>）是 **backend-only**，**可能没有公网出网能力**（之前下模型时是经入口机代理）。—— *实测证明此顾虑不成立，node2 可直连 Agent。*
- 影响：`/ws/chat` 经 nginx LB 可能被分到 node2，而 node2 **连不上远端 Agent**（出网失败）→ 该连接对话报错。—— *实测未发生，node2 对话正常。*
- ASR/TTS 不出网，所以双机 LB 一直没暴露这个问题；**聊天是第一个需要出网的业务**。

**应对方案（择一，落地前与用户确认）**：

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 聊天只钉 node1**（设计期推荐 v1，**现已不需要**） | nginx 对 `/ws/chat` 单独 `upstream` 只含 node1；ASR/TTS 仍双机 LB。 | 最简单、最稳；聊天容量受单机限制。**因 node2 实测能出网，已放开为双机 LB，不再采用此方案。** |
| B. 给 node2 配出网 | 开 NAT 网关 / 公网带宽给 node2。 | **涉及购买云资源 / 改网络**，需用户决策且花钱，AI 不代办。—— *node2 本就能出网，无需此项。* |
| C. node2 经 node1 出网 | node2 的 Agent 请求走 node1 做正向代理。 | 多一跳、多一处故障点；node1 成为出网单点。—— *无需，node2 直连即可。* |

> ~~v1 建议走 A~~ → **实际采用双机 LB**：`/ws/chat` = `backend_chat`(node1 + node2)，与 ASR/TTS 一致。无会话粘性（一次对话=一条长连接，天然粘在建连的那台，无跨机一致性问题）。

nginx 片段（方案 A，示意）：
```nginx
upstream chat_backend { server backend:8000; }   # 仅 node1
location /ws/chat {
    proxy_pass http://chat_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;   # 长连接对话, 拉长读超时
}
```

---

## 8. 安全与配置

- **API key 不入库**：`agent_api_key` 仅写 node-local `deploy/.env.deploy`（已在 sync 排除清单），随容器 env 注入。
- **出网目标白名单**：`agent_endpoint` 固定一个可信地址；不接受前端传入任意 URL（防 SSRF）——`system_prompt` 可由前端覆盖，但 endpoint/key/model 只由服务端配置决定。
- **backend 仍无鉴权 + 仅内网放行**：维持现状，8000 不公网暴露，对外只走 node1 的 nginx。
- **隐私**：对话音频 / 文本默认不落盘（v1 内存即用即弃）；如需排障日志，仅记录文本摘要，不存原始音频。

---

## 9. 分阶段实施计划

> 每一步都先本地（CPU/stub Agent）验证，再上 node1。涉及装包（httpx）与业务代码改动，**动手前先告知确认**。
>
> **【2026-07-04 落地状态】**：Step 0–4 已全部完成并上线。**Step 4 的「nginx 钉 node1」已不适用**——node2 实测能出网连 Agent，`/ws/chat` 已放开为 node1+node2 双机 LB（见 §7 更正）。Step 5 的「barge-in」也已落地（见 §12），且做成前端 start 帧 `barge_in` 逐连接可选、默认关。下列步骤保留为实施历史。

### Step 0 — 准备（无代码）
- 用户提供：远端 Agent 的 `endpoint` / `api_key` / `model`（或确认用现成的 DashScope/方舟兼容端点）。
- ~~确认 node2 出网策略 = §7 方案 A（聊天钉 node1）~~ → **实际：node2 能出网，聊天走双机 LB（§7 更正）**。

### Step 1 — 后端 AgentClient + 单测
- 新增 `agent_client.py` + 配置项；用一个 OpenAI 兼容 mock / 真实端点跑通 SSE 解析。
- 验收：给定 messages，能流式拿到 token；超时 / 5xx 能正确抛错。

### Step 2 — ConversationOrchestrator + `/ws/chat`
- 新增 orchestrator + 路由 + main 装配；本地用 stub ASR/TTS + 真/mock Agent 跑通 turn 状态机。
- 验收：本地 WS 客户端发 PCM → 拿到 user_final → assistant_partial → 二进制音频 → assistant_done。

### Step 3 — 前端 ChatPage
- 新增 `ChatPage.tsx` + `openChatStream` + App tab；复用 recorder/player/QosBadge。
- 验收：浏览器里完成一轮语音对话（本地或 node1）。

### Step 4 — 上线 + nginx（**实际为双机 LB，非钉定**）
- backend 重建（含聊天代码）；nginx `/ws/chat` → `backend_chat`（node1 + node2:8000）双机 upstream；两机 env 配 Agent。
- 验收：公网 https 入口完成多轮语音对话（node1/node2 均可）；ASR/TTS 双机 LB 不受影响；QoS / 时延记录如实留档。

### Step 5（已部分落地）
- ~~node2 出网打通后放开聊天 LB~~ → **已放开（node2 本就能出网）**；**barge-in 已落地**（§12，前端可选/默认关）；对话持久化、AEC 硬件级回声消除仍为后续可选项。

---

## 10. 回退方案

- **总开关**：`chat_enabled=false`（或不配 `agent_endpoint`）→ `/ws/chat` 直接回 `{type:error, code:"unavailable"}`，前端 tab 灰显。ASR/TTS 完全不受影响。
- **nginx 回退**：移除 `/ws/chat` location 即彻底下线聊天入口，其余路由不变。
- **代码隔离**：聊天为纯新增文件（agent_client / conversation / routes_chat / ChatPage / openChatStream），不修改 ASR/TTS 既有引擎与路由；回退 = 删新增 + 撤 include/tab。
- **远端 Agent 故障**：单连接报错回到 LISTENING，不影响其他连接，不污染整机就绪（Agent 不进 readyz）。

---

## 11. 待用户确认事项（落地前）

> **【2026-07-04：本节为落地前的待确认清单，现已全部落定】** 1 已配（阿里云百炼 qwen-plus）；2 经实测 node2 能出网 → 聊天走**双机 LB**（非方案 A 钉 node1，见 §7 更正）；3 httpx 已引入；4 已编码上线。

1. **远端 Agent 接入信息**：endpoint / api_key / model 三件套（或指定用哪个兼容端点）。—— *已配阿里云百炼兼容端点 + qwen-plus。*
2. ~~**node2 出网策略**：确认采用 §7 方案 A（聊天 v1 钉 node1）~~ → **node2 实测能出网，聊天采用双机 LB（§7 更正）**。
3. **装包确认**：后端引入 `httpx`（若现仓库未含）。—— *已引入。*
4. **是否现在进入编码**：本文件为设计稿，按用户既有约束「业务代码改动须经确认」，批准后再按 §9 实施。—— *已批准、已实施上线。*

---

## 12. barge-in（说话打断）落地（2026-07-04）

> v1 设计时（决策③）把 barge-in 推迟，理由是单 T4 无 AEC、前端外放会被麦克风收到造成自打断。本次在既有语音聊天实现上，把 barge-in 作为**默认关闭的可开关增强**落地：用「浏览器端 AEC + ASR 识别到实际文字才打断」双保险规避自打断；开启后即可在 AI 说话时插话打断。

### 用户确认的三项决策
| 决策点 | 选择 | 说明 |
|---|---|---|
| 回声处理 | **浏览器端 AEC** | 前端 `getUserMedia` 开 `echoCancellation/noiseSuppression/autoGainControl`；不强制耳机，但戴耳机体验最佳。 |
| 打断语义 | **立即停播 + 中止生成** | 检测到打断即停前端外放、`aclose` 远端 Agent SSE、丢弃排队 TTS；已说出的半句写回上下文，马上回 LISTENING 接住新话。 |
| 开关/默认 | **前端可选，默认关** | `chat_barge_in` env 为**服务端默认**（部署置 false）；**前端 start 帧 `barge_in` 字段可逐连接覆盖**，用户在「高级设置」勾选即开。 |

### 开关粒度：env 默认 + 前端逐连接覆盖（2026-07-04 增强）
最初 barge-in 只由服务端 env `chat_barge_in` 控制（部署级）。本次改为**前端用户可选**：

- 后端 [conversation.py](../../backend/app/orchestration/conversation.py#L95-L97) 解析 start 帧：`if "barge_in" in start: self._barge_on = bool(start["barge_in"])` —— 前端显式传值即覆盖 env 默认；不传则沿用 `settings.chat_barge_in`。
- 前端 [client.ts](../../frontend/src/api/client.ts#L155) `ChatStartOptions.bargeIn` → start 帧 `barge_in` 字段；[ChatPage.tsx](../../frontend/src/pages/ChatPage.tsx) 「高级设置」新增复选框「说话打断 (barge-in)：AI 回答时可插话打断，建议戴耳机」，`bargeIn` state 透传。
- 线上 node1 env 置 `CHAT_BARGE_IN=false`（默认关），实际开关交给前端每次连接选择。非前端客户端（不传 `barge_in`）则按 env 默认走。

### 回复音色选择（2026-07-15 增强）

- 后端 `ConversationSession` 已支持 start 帧 `voice` 字段覆盖 `agent_tts_voice`；前端「高级设置」新增「回复音色」下拉框，启动连接时把所选音色透传到 `openChatStream({ voice })`。
- 音色列表复用 `/api/v1/models` 的 TTS `languages` 字段；线上历史女声音色 id `default` 在 UI 展示为「中文女」，另有真实注册男声音色「中文男」。
- 音色按连接生效：开始对话后固定本次连接的 TTS 音色，需切换时先结束对话再重新选择。

### 后端改动
- **配置** [config.py](../../backend/app/config.py#L147-L152)：新增 `chat_barge_in: bool = False`（总开关）、`chat_barge_in_min_chars: int = 2`（打断判据：RESPONDING 期间 ASR partial 累计实际字 ≥ 该值才触发，滤残余回声/噪声）。
- **状态机** [conversation.py](../../backend/app/orchestration/conversation.py)：
  - `_read_loop`：barge-in 开启且 `state==responding` 时，麦克风 PCM 改路由到独立 `_barge_q`（关闭时维持 v1 丢弃行为，零影响）。
  - `_respond`：进入 RESPONDING 时起 [_watch_interrupt](../../backend/app/orchestration/conversation.py#L326-L349) task —— 消费 `_barge_q` 跑轻量流式 ASR，识别到 ≥`min_chars` 个非空白字即 set `self._interrupt`。
  - Agent token 循环与 `speak()` 的音频下发循环**每步检查** `self._interrupt`：命中则关闭 TTS 生成器（停下发）、`break` 出 token 循环、`aclose` Agent SSE（停远端生成）。
  - 收尾：已生成的 `assistant_full` 写回 `messages`（保多轮连贯），下发新帧 `{type:"interrupted", node}`（**不发** `assistant_done`），回 LISTENING。
- **判据设计**：用「识别到实际文字」而非纯 VAD 能量——依赖浏览器 AEC 压掉外放回声后，残余回声不足以形成有效文字，从而滤掉自打断。watcher 失败不影响主流程（最坏=本轮不可打断）。

### 前端改动
- [recorder.ts](../../frontend/src/audio/recorder.ts#L32-L41)：`getUserMedia` 开 AEC/降噪/自动增益。
- [player.ts](../../frontend/src/audio/player.ts#L35-L47)：`StreamingPcmPlayer` 保留已排 `AudioBufferSourceNode` 引用，新增 `stop()` 逐个 `.stop()` 并复位播放游标——实现打断时**立即静音**（原实现只能等排队音频自然播完）。
- [client.ts](../../frontend/src/api/client.ts#L166)：`ChatHandlers` 加 `onInterrupted`，onmessage 分发 `interrupted` 帧。
- [ChatPage.tsx](../../frontend/src/pages/ChatPage.tsx#L95-L102)：收到 `interrupted` → `player.stop()` + 把已说半句落为定稿气泡 + 清 assistantPartial。

### 协议新增
- 上行 start 帧新增可选字段 `barge_in: bool`：前端逐连接选择开/关，覆盖服务端 env 默认（不传则用 env 默认）。
- 下行帧 `{type:"interrupted", node}`：本轮被用户打断，前端据此停播、收尾字幕；该轮**不再有** `assistant_done`。

### 关键权衡（如实留档）
- **GPU 共享**：RESPONDING 期间打断检测 ASR 与 TTS 播放同占单 T4，检测用轻量 paraformer-online partial，命中即停 TTS，抢占窗口短——会让 TTS 首包略慢，可接受。
- **AEC 局限**：浏览器 AEC 对流式外放消除不完全稳定（不同浏览器差异大），故用文字判据 + `min_chars` 双保险，并建议戴耳机。这也是**默认关**的原因。
- **打断粒度**：token 循环与音频下发循环两处都查 `_interrupt`，最坏延迟一个 TTS 音频块（几十 ms 级），体感即时。

### 验证 + 部署
- 单测（[test_conversation.py](../../backend/tests/test_conversation.py)）：① barge-in 开启时 responding 期间来文字 partial → 触发 `interrupted`、Agent 流中止、不发 `assistant_done`；② 关闭时整轮正常完成；③ start 帧 `barge_in` 双向覆盖 env 默认。本地全套 **82 passed**，前端 `tsc -b && vite build` 通过。
- **node1**（入口机，公网 IP 因实例重启变为 **<NODE_PUBLIC_HOST>**，内网/hostname 不变）：rsync 后端代码 + 前端 dist，`.env.deploy` 置 `CHAT_BARGE_IN=false`（服务端默认关，改前备份），rebuild backend+web（BUILD_EXIT=0，10s healthy）。
- 容器内 `ws://127.0.0.1:8000/ws/chat` 实测：① **env 默认关 + start `barge_in:true`** → responding ~1s 后灌新语音收到 `interrupted`、Agent 流中止（证明前端可覆盖开启）；② **env 默认关 + 不传 flag** → 走完整 turn（`assistant_done`，默认行为不变）。
- **node2 同步**：barge-in 全套代码已同步 node2（见下）；且经实测 node2 能出网连 Agent，`/ws/chat` 已是双机 LB，barge-in 在 node2 同样生效。

### node2 代码同步（2026-07-04）
barge-in 全套代码已同步到 node2（<NODE_PUBLIC_HOST>），两机 backend 代码一致：

- 方式：rsync `backend/app` → node2，用 **overlay Dockerfile**（`FROM asr-tts-backend:latest` + `COPY app`）增量构建（纯 Python 无新依赖，规避 node2 Docker Hub 不可达；见《水平扩容Runbook-单机LB骨架》§3.6）。BUILD_EXIT=0（零联网），recreate 后 15s healthy。
- 校验：node2 容器内 `chat_barge_in default=False`、`min_chars=2`、start 帧 `barge_in` 覆盖逻辑在位，四种「env 默认 × start flag」组合覆盖结果与 node1 完全一致（False+True→True / True+False→False / 默认沿用 env）。
- **node2 实际可用**（修正上一版文档的错误表述）：经实测 node2 能端到端连远端 Agent（见 §7 更正），`/ws/chat` 已是 node1+node2 双机 LB。公网 LB 8 轮测试 node1/node2 交替、每轮对话都完整跑通。故 barge-in 在 node2 **同样生效**，不再是「冷备/预置」。
- node2 `.env.deploy` 未设 `CHAT_BARGE_IN`（默认 false），与 node1 一致；前端 start 帧 `barge_in` 逐连接控制，两机行为一致。

### 与 §7 部署约束的关系
barge-in 不改变聊天的部署拓扑：`/ws/chat` 已是 node1+node2 双机 LB（见 §7 更正），barge-in 只在两机各自的 `/ws/chat` 连接上增加半双工能力，nginx 配置无需变动、两机行为一致。

---

## 13. 噪声误触发修复：前端能量 VAD + 后端有效发言门控（2026-07-04）

> 用户反馈：语音聊天很多时候没说话也会被环境噪声误录入、触发一整轮对话。排查确认这是**能力缺口**而非 bug。

### 根因
- **前端无 VAD**：[recorder.ts](../../frontend/src/audio/recorder.ts) 原本无条件把每块 PCM 上送，静音/噪声照发。
- **后端 VAD 只管端点不管内容**：[funasr_engine.py](../../backend/app/engines/funasr_engine.py) 的 fsmn-vad 仅用于判断「句子何时结束」触发定稿；真正出字的 paraformer-online **对每块无条件出字**，环境噪声照样被转成文字。
- **聊天无过滤即触发**：[conversation.py](../../backend/app/orchestration/conversation.py) `_listen` 收到第一个 `is_final` 且文本非空即当「用户说完一句」交给 Agent——噪声误识别的一两个字就能凭空触发一轮 ASR→LLM→TTS。

### 修复（前后端双层，互补）
**后端·有效发言门控**（第二道，语义层，两机生效）：
- 配置 [config.py](../../backend/app/config.py#L153-L158)：`chat_min_speech_chars`（定稿去标点/空白后实际字数下限，默认 2）、`chat_min_speech_ms`（该段语音累计时长下限，默认 300ms）。
- [conversation.py](../../backend/app/orchestration/conversation.py) `_listen` 改为**循环到「有效」final 才交 Agent**：拿到 `is_final` 时校验 `_is_valid_speech`（实际字数 ≥ 阈值 **且** 语音时长 ≥ 阈值），不满足则丢弃该「句」、重置计数、继续听，不下发 `user_final`、不触发 Agent。时长用 `_listen` 内累计送入 ASR 的样本数估算（`ASRPartial` 不带时长）。
- `_real_char_count`：按 Unicode 类别排除空白与标点/符号（P*/S*），滤掉「纯标点」「单字噪声」。barge-in 判据也复用此函数（更准）。

**前端·能量 VAD 门控**（第一道，省带宽/省后端算力，默认开）：
- [recorder.ts](../../frontend/src/audio/recorder.ts#L82-L101) `MicRecorder` 加 `gateVad` 选项：每块算 RMS，维护自适应噪声底，超「噪声底 + margin(默认 6dB)」判为有声才上送；掉阈值后 **hangover(默认 400ms)** 继续送，避免切句尾；静音期完全不发。
- [ChatPage.tsx](../../frontend/src/pages/ChatPage.tsx) 「高级设置」加复选框「静音门控 (VAD)：仅在检测到说话时上送，过滤环境噪声」，**默认开**。

### 取舍（如实留档）
- 前端能量 VAD **不能区分稳定人声背景**（旁人说话/电视声）——那种靠后端语义门控兜底，所以两层互补。
- 门控阈值调太高会吞掉真实短指令（「好」「停」）。默认取保守值（后端 2 字/300ms、前端 6dB/400ms hangover），均可配；若用户反映短指令被吞，调低 `chat_min_speech_chars`/`chat_min_speech_ms`。
- 单字有效指令（如「停」1 字）在默认 `min_chars=2` 下会被后端门控挡掉——当前语音聊天以多字自然语句为主，如需支持单字命令再单独放宽。

### 验证 + 部署
- 后端单测（[test_conversation.py](../../backend/tests/test_conversation.py)）：`_real_char_count` 去标点计数、`_is_valid_speech` 门控、噪声短 final 被丢弃而有效 final 通过。本地全套 **85 passed**，前端 `tsc -b && vite build` 通过。
- **node1**（<NODE_PUBLIC_HOST>）：rebuild backend+web；**node2**（<NODE_PUBLIC_HOST>）：overlay 增量构建。两机 `min_chars=2 min_ms=300` 生效。
- 公网 LB 实测：① 200ms 微噪声 → **无 `user_final`、不触发对话**（门控丢弃）；② 完整真人语音 → `user_final` 有效、整轮 `assistant_done` 跑通；③ LB 4 轮 node1/node2 交替、真人语音每轮都完整跑通。
- 前端能量 VAD 的浏览器实测（说话/静音/背景噪声）受限于无头环境未在本轮跑，代码已上线、逻辑已单测覆盖 RMS 判据；建议在真实浏览器点开「静音门控」验证一次。

## 14. 咳嗽误触发修复：SenseVoice 事件标签层（2026-07-05）

> 用户反馈：§13 的双层门控上线后，**咳嗽声仍会被误录入**。经三轮 node1 真机取证（临时只读日志探针，验证后即撤），定位为「非语言声」穿透了「量」判据门控，补 SenseVoice 事件标签「质」判据后闭环。

### 根因（取证链）
聊天定稿走 SenseVoice offline（`AGENT_ASR_MODEL=funasr-streaming` → 句末 SenseVoice 重解码）。取证发现咳嗽有两种命运，都不被 §13 的字数/时长门控可靠拦住：
- **形态 A**：SenseVoice 识别为 `<|Cough|>` 事件标签 + 硬转拟声词（实测原始输出 `<|zh|><|EMO_UNKNOWN|><|Cough|><|withitn|>嗯哼。`）。经 `rich_transcription_postprocess` 后 `<|Cough|>` 变成 emoji `😷`、正文剩「嗯哼」**2 个真汉字** → 过字数门控（≥2）→ 误触发。
- **形态 B**：SenseVoice 定稿输出**空**（`raw=''`），2pass 的 `_correct` 回退到第一遍 paraformer-online 的临时字（如「嗯」「啊」「哼」1 字）→ 被字数门控（<2）兜住。

关键事实：SenseVoice item **不输出置信度 score**（字段仅 `key`/`timestamp`），故「置信度阈值」方案不可行；但富文本**事件标签**（`<|Cough|>` 等）是区分「非语言声」vs「真实语音 `<|Speech|>`」的干净信号——只是原本被后处理抹成 emoji 丢弃了。

### 修复：事件标签层（第三道，引擎层，`_postprocess` 之前）
- **配置** [config.py](../../backend/app/config.py#L49-L51)：新增 `funasr_drop_nonspeech_events: bool = True`（默认开，可 env 关回退旧行为）。
- **引擎** [funasr_engine.py](../../backend/app/engines/funasr_engine.py#L17-L23)：模块级 `_NONSPEECH_EVENT_TAGS`（`BGM`/`Applause`/`Laughter`/`Cry`/`Sneeze`/`Breath`/`Cough`）；`transcribe` 循环里对 SenseVoice 原始富文本先过 `_is_nonspeech_event`，命中即该 item 判噪声、返回空文本，交由后端字数门控（0 字）丢弃。
- **保守放行**：原始文本若**同时含 `<|Speech|>`** 则放行（说话中夹杂咳嗽仍按说话处理），避免误伤真实发言。

### 最终防护矩阵（三层，覆盖各咳嗽形态）
| 咳嗽被识别成 | 拦截层 | 触发对话 |
| --- | --- | --- |
| `<\|Cough\|>` + ≥2 字拟声（形态 A） | 事件标签层（本次新增） | 否 |
| 空 / 单字拟声「嗯」「啊」「哼」（形态 B） | 后端字数门控（§13，≥2 字） | 否 |
| 含 emoji `🤧` 的单字 | 后端字数门控（emoji 属符号，不计字数） | 否 |

**理论漏网**：咳嗽既被标 `<|Speech|>`（非 `<|Cough|>`）又被硬转 ≥2 个真汉字——事件层保守放行、字数层也放行。三轮实测未复现，属低概率；若后续复现再加拟声词黑名单兜底。

### 验证 + 部署
- 后端单测（[test_funasr_events.py](../../backend/tests/test_funasr_events.py)，新增 6 例）：咳嗽/喷嚏/BGM/掌声事件被丢、`<|Speech|>` 正文透出、Speech+Cough 共现保守放行、开关关闭回归旧行为、混合批只丢非语音段。本地全套 **91 passed**。
- **node1**（<NODE_PUBLIC_HOST>）：docker cp 增量部署 `funasr_engine.py`+`config.py`，重启 backend healthy，`funasr_drop_nonspeech_events=True` 生效。取证探针验证后已全部撤除，容器代码 md5 与本地干净版一致。
- 三轮真机取证结论：① 孤立咳嗽（`嗯哼`2字，`<|Cough|>`）→ 事件标签层可拦；② 孤立咳嗽（空/单字）→ 字数门控兜住；③ 咳嗽+说话连读（VAD 未断句）→ 合成 `<|Speech|>哼今天天气不错`，按正常发言通过（合理）。所有孤立咳嗽均未触发对话，正常说话正常触发。
- **node2 同步**（2026-07-05）：事件标签层已按 §13 overlay/docker cp 增量方式同步至 node2（<NODE_PUBLIC_HOST>），两机容器 `funasr_engine.py`+`config.py` md5 逐字节一致（`89c949…`/`be057c…`），`funasr_drop_nonspeech_events=True` 双机生效。**双机防护完全对齐**：LB 命中任一节点，咳嗽等非语言声均走三层防护（事件标签层 → 字数门控 → 时长门控）。

## 15. 原生端到端语音模式（2026-07-16）

> 为降低级联链路延迟并保留语气、停顿、笑声等副语言信息，`/ws/chat` 新增原生语音 Provider。现有级联链路继续保留，作为稳定可控基线。

### 设计

- `/ws/chat` 首帧新增 `mode`：`cascade` 走现有 ASR→Agent→TTS，`native` 走 Qwen-Omni-Realtime；未传时仍默认为 `cascade` 兼容旧客户端。
- 前端通过 `GET /api/v1/chat/models` 动态发现可用模式、模型、音色和能力。若后端已配置原生模型，新版前端默认选中「原生语音」；未配置时自动回退级联。
- 原生 Provider 在后端保存 API Key 并代理到厂商 WebSocket，浏览器不直连厂商服务，不暴露密钥。
- 原生 Provider 有独立并发闸，不占本地 ASR/TTS GPU 信号量。

### 音频链路

- 原生模式持续上传 20 ms PCM 音频块，强制关闭前端能量 VAD，避免本地门控丢失停顿、笑声、呼吸等副语言信息。
- 采集优先使用 `AudioWorklet`，旧浏览器回退 `ScriptProcessorNode`。
- 播放端使用 100 ms 启动缓冲和 120 秒安全队列上限。此前 2 秒队列上限会在原生模型快速返回长回复音频时丢弃后续块，表现为“文本继续输出但音频只播前一两句”，已修复。
- 浏览器播放欠载和队列丢块会作为客户端指标回传到后端 Prometheus。

### VAD 与打断

- `QWEN_OMNI_TURN_DETECTION` 默认 `semantic_vad`。
- `QWEN_OMNI_SILENCE_MS` 默认 2000 ms，用于降低句中自然停顿被服务端 VAD 切断的概率；如需更快端点，可在 1200-2000 ms 间灰度。
- 原生模式下 barge-in 始终可用，建议戴耳机降低回声触发打断的概率。

### 配置与验收

配置项见《[配置说明](../configuration.md)》的「原生端到端语音」章节；A/B 脚本、指标和灰度门槛见《[原生端到端语音对话升级方案](native-speech-upgrade.md)》。

## 16. 停止语义、Provider 默认值与连接恢复（2026-07-18）

本节为当前实现，覆盖 §12 中“只在 RESPONDING 阶段由说话触发打断”的早期语义。

### 统一取消协议

前端在 THINKING 或 RESPONDING 状态显示“停止当前回复”，上行：

```json
{"type":"cancel_response"}
```

- **级联模式**：等待 Agent 首 token 的 task 与取消事件并行；用户可在“思考中”停止，无需等待远端先返回。RESPONDING 时关闭当前 TTS 异步流和 Agent SSE。
- **原生模式**：立即停浏览器播放器，同时向厂商上游发送 `response.cancel`。上游已结束或断开时按幂等取消处理，不把取消失败升级成会话故障。
- 两种模式都下发 `{"type":"interrupted","text":"...","node":"..."}`，不再发送本轮 `assistant_done`，状态回到 listening。
- 级联模式被打断时，只把已经完整流式下发的句子写入多轮历史；模型生成但用户没有听到的后续文本不会进入上下文。该规则优先于 §12 早期“写回全部 `assistant_full`”的描述。

### Provider 能力目录

`GET /api/v1/chat/models` 除模型、音色和输入格式外，还返回：

| 字段 | 含义 |
| --- | --- |
| `default_barge_in` | Provider 推荐的说话打断默认值 |
| `default_vad_gate` | Provider 推荐的前端能量 VAD 默认值 |
| `default_capture_profile` | `natural` 或 `noise_reduction` |
| `vad_silence_ms_options` | 可选服务端端点档位 |
| `default_vad_silence_ms` | 推荐端点静音时长 |

前端切换模式/模型时完整应用这组默认值。原生模式推荐自然采音、关闭本地门控和开启打断；级联模式推荐嘈杂环境采音和本地 VAD。高级设置仍允许用户按场景调整支持的选项。

### 连接与播放

- Chat 使用 `idle -> connecting -> connected` 状态机，10 秒建连超时；只有收到 `ready` 后才打开麦克风。
- 非预期 WebSocket 关闭会停止麦克风、播放器并恢复 UI，不会永久停在“已连接/忙碌”状态。
- 播放欠载和丢块指标在每个 `assistant_done` / `interrupted` 回合上报并清零，不再只在整场会话结束时累计上报。
- native 不可用且目录中存在 cascade Provider 时，错误区提供“切换到级联模式”入口；不会在同一连接内自动迁移上下文。
- `chat_sessions_total{status="disconnected"}` 区分异常断线；主动或说话打断继续记录为 `chat_turns_total{status="interrupted"}`。
