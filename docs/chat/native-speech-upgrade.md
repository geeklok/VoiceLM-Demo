# 原生端到端语音对话升级方案

> 状态：代码已落地，默认关闭；配置百炼 Workspace 地址与 API Key 后前端默认优先使用原生语音模式，并可与级联模式做线上 A/B。

## 1. 架构

`WS /ws/chat` 保持为统一入口，首帧 `start.mode` 选择 Provider：

```text
Browser
  └─ WS /ws/chat
       ├─ mode=cascade
       │    └─ FunASR -> OpenAI-compatible Agent -> CosyVoice2
       └─ mode=native
            └─ Qwen-Omni-Realtime 双向 WebSocket
```

- 未传 `mode` 时默认 `cascade`，兼容旧客户端；新版前端会通过能力发现优先选择已配置的 `native` 模式。
- 会话内不自动切换 Provider。原生服务失败时返回错误，由用户重新连接级联模式，避免上下文错乱和重复回复。
- API Key 只存在于后端环境变量，浏览器不直接连接厂商。
- 原生 Provider 使用独立并发闸，不占本地 ASR/TTS GPU 信号量。

## 2. 前端行为

- 工具栏通过 `GET /api/v1/chat/models` 动态展示实际可用的模式、模型和音色；如果后端暴露原生模型，默认选中原生语音，否则回退级联。
- 原生模式持续上传 20 ms PCM 块，强制关闭前端能量 VAD，保留停顿、笑声和呼吸等信息。
- 采集优先使用 `AudioWorklet`；旧浏览器回退 `ScriptProcessorNode`。
- 播放器使用 100 ms 启动缓冲和 120 秒安全队列上限。超限丢块和播放欠载会在结束会话时回传后端。
- 两种模式继续复用浏览器 AEC、流式字幕、立即停播和现有消息气泡。

## 3. 配置

在 `deploy/.env.deploy` 配置：

```bash
CHAT_ENABLED=true
QWEN_OMNI_ENABLED=true
QWEN_OMNI_ENDPOINT=wss://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
QWEN_OMNI_API_KEY=<写入节点本地配置>
QWEN_OMNI_MODEL=qwen3.5-omni-flash-realtime
QWEN_OMNI_MODEL_ALLOWLIST=["qwen3.5-omni-flash-realtime","qwen3.5-omni-plus-realtime"]
QWEN_OMNI_VOICES=["Tina"]
QWEN_OMNI_DEFAULT_VOICE=Tina
QWEN_OMNI_CONCURRENCY=4
QWEN_OMNI_TURN_DETECTION=semantic_vad
QWEN_OMNI_VAD_THRESHOLD=0.5
QWEN_OMNI_SILENCE_MS=2000
```

`QWEN_OMNI_SILENCE_MS` 默认取 2000 ms，用于降低句中自然停顿被服务端 VAD 切成多轮的概率；如果响应显著变慢，可按场景在 1200-2000 ms 间灰度。配置后需 recreate backend，而不是只做 restart。两台节点都配置后，现有 `/ws/chat` 双机 LB 可继续使用。

## 4. 可观测性

新增指标均使用受控的 `provider/model/status` 标签，不写会话 ID 或文本：

| 指标 | 含义 |
| --- | --- |
| `chat_sessions_total` | 会话成功、失败、不可用和繁忙数 |
| `chat_turns_total` | 回合成功、失败、打断数 |
| `chat_endpoint_seconds` | 用户语音结束到端点定稿 |
| `chat_first_audio_seconds` | 定稿到首块回复音频 |
| `chat_turn_seconds` | 定稿到回复生成完成 |
| `chat_interrupt_seconds` | 确认打断到回复停止 |
| `chat_audio_seconds_total` | 输入、输出音频时长 |
| `chat_tokens_total` | 厂商返回的文本/音频 Token，用于按价目表核算费用 |
| `chat_provider_errors_total` | Provider 错误码 |
| `chat_playback_underruns_total` | 浏览器播放欠载 |
| `chat_playback_dropped_chunks_total` | 有界缓冲为限制积压而丢弃的块 |

## 5. 同语料 A/B

准备 16 kHz、单声道、16-bit PCM WAV。每条录音开头和结尾不要保留大段静音。建议每类至少 10 条：

- 普通短问答；
- 犹豫、句中停顿和自我修正；
- 笑声后说话、说话中夹笑；
- 低音量和嘈杂背景；
- 开心、失落、激动等情绪；
- 需要精确事实或工具调用的业务任务；
- AI 播放中插话的打断用例。

复制并修改 [语料清单示例](ab-corpus.example.json)，然后执行：

```bash
backend/.venv/bin/python scripts/chat_ab_loadtest.py \
  --url wss://your-domain.example.com/ws/chat \
  --manifest docs/chat/ab-corpus.json \
  --cascade-model qwen3.7-plus \
  --native-model qwen3.5-omni-flash-realtime \
  --rounds 5 \
  --insecure \
  --output /tmp/chat-ab-results.json
```

脚本对同一 WAV 交替执行两种模式，降低时段偏差，并输出首声 p50/p95、总耗时和成功率。主观指标由双盲评审记录：

| 维度 | 记录方式 |
| --- | --- |
| 副语言保真 | 是否正确理解停顿、笑声、犹豫和情绪 |
| 回复自然度 | 韵律、情绪匹配、是否像逐句 TTS 拼接 |
| 内容正确性 | 事实、指令遵循和上下文一致性 |
| 任务成功率 | 工具任务是否得到正确最终结果 |
| 打断体验 | 是否误打断、漏打断、停播是否及时 |

## 6. 灰度门槛

原生模式满足以下条件后再扩大流量：

- 首声延迟 p50 相对级联下降至少 35%；
- 首声延迟 p95 不高于级联；
- 打断延迟 p95 小于 300 ms；
- 协议成功率不低于 99%；
- 业务任务成功率相对级联下降不超过 5 个百分点；
- 副语言与自然度双盲胜率超过 60%；
- `chat_playback_underruns_total` 和丢块率低于 1% 回合；
- 根据 `chat_tokens_total` 核算后的单回合成本在预算内。

未达标时继续保持级联为默认。原生模式适合自然聊天，级联模式继续承担强调可控性、热词、RAG 和复杂工具链的任务。

## 7. 本地自托管边界

现有每台 T4 仅 15360 MiB，且 ASR/TTS 稳态已占约 5.9 GB，不应混部端到端语音模型。后续如新增至少 32 GB、优先 48 GB GPU，可在独立服务上评估 MiniCPM-o，再通过同一个 `ChatProvider` 契约接入；不要把其依赖装入当前 FunASR/CosyVoice 容器。

## 8. 已知问题与处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 文本持续输出，但音频只播前一两句 | 原播放队列上限过小，原生模型返回音频快于浏览器实际播放时，后续块被当作积压丢弃 | Chat 播放器改为 100 ms 启动缓冲 + 120 秒安全队列上限，并保留丢块指标 |
| 用户一句话被切成多轮 | 服务端 VAD 静音端点过短，句中停顿被判为结束 | `QWEN_OMNI_SILENCE_MS` 默认调到 2000 ms |
| 原生模式下本地静音门控不可勾选 | 原生语音需要保留停顿、笑声、呼吸等副语言信息 | 原生模式强制关闭前端能量 VAD，交由模型端 VAD/语义 VAD 处理 |
