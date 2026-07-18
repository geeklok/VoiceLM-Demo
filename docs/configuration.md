# 配置说明

配置通过环境变量注入；本地开发可写入 `backend/.env`，Docker 部署可写入 `deploy/.env.deploy`。真实配置文件已被 `.gitignore` 忽略，不要提交到仓库。

## 配置文件

| 文件 | 用途 | 是否提交 |
| --- | --- | --- |
| `backend/.env.example` | 后端本地配置模板 | ✅ |
| `backend/.env` | 后端本地真实配置 | ❌ |
| `backend/scripts/env.local.example` | 本地真实模型配置模板 | ✅ |
| `deploy/.env.deploy.example` | 容器部署配置模板 | ✅ |
| `deploy/.env.deploy` | 容器部署真实配置 | ❌ |
| `deploy/sync.env.example` | rsync 部署脚本配置模板 | ✅ |
| `deploy/sync.env` | rsync 目标机器、SSH Key 等真实配置 | ❌ |

## ASR

- `ASR_ENGINE`：`stub` 或 `funasr`。
- `FUNASR_MODEL`：主 ASR 模型 ID 或本地模型目录。
- `FUNASR_STREAMING_MODEL`：流式 Paraformer 模型；留空则不注册真流式引擎。
- `FUNASR_SEACO_MODEL`：支持热词偏置的 SeacoParaformer；留空则不注册热词模型。
- `DEFAULT_ASR_MODEL`：默认对外 ASR 模型名；留空使用首个注册模型。

文件上传按 1 MiB 分块读取，超过 `MAX_UPLOAD_BYTES` 会立即返回 413，不会先把超大文件完整读入内存。文件解码通过线程池执行，避免同步 `ffmpeg` 阻塞 WebSocket 事件循环。客户端显式传入未注册模型时返回 400，不再静默回退默认模型。

## TTS

- `TTS_ENGINE`：`stub` 或 `cosyvoice`。
- `COSYVOICE_REPO_DIR`：CosyVoice 仓库根目录。
- `COSYVOICE_MODEL`：CosyVoice2 模型 ID 或本地模型目录。
- `COSYVOICE_DEFAULT_VOICE`：默认音色名，必须存在于 `COSYVOICE_VOICES`。
- `COSYVOICE_VOICES`：JSON，格式为 `音色名 -> {audio_path, prompt_text}`。参考音频建议为 16 kHz WAV，并确保 `prompt_text` 与音频内容一致。
- `TTS_SENTENCE_STREAM`：长文本是否按句流式合成。
- `TTS_MAX_SENTENCE_CHARS` / `TTS_MIN_SENTENCE_CHARS`：分句后的最大字符数和短句合并下限。
- `TTS_STREAM_QUEUE_CHUNKS`：同步 CosyVoice 推理线程与异步 WebSocket 之间的有界队列水位，默认 8。值过大增加取消后的积压，值过小可能放大生产/消费抖动。

示例：

```bash
COSYVOICE_DEFAULT_VOICE=中文女
COSYVOICE_VOICES='{"中文女":{"audio_path":"/voices/default.wav","prompt_text":"这里填女声参考音频准确的文字内容"},"中文男":{"audio_path":"/voices/male.wav","prompt_text":"这里填男声参考音频准确的文字内容"}}'
TTS_STREAM_QUEUE_CHUNKS=8
```

服务只接受实际注册成功的音色。历史展示名 `中文女` 可映射到配置的默认音色；其他未知音色返回 400 或 WebSocket `bad_request`，不会静默换成默认声音。流式客户端断开时，服务会关闭异步生成器、通知同步推理线程停止并等待其退出后再释放 GPU slot。

## Chat / 远端 Agent

- `CHAT_ENABLED`：是否启用语音聊天。
- `AGENT_ENDPOINT`：OpenAI-compatible API base URL，可带或不带 `/v1`。
- `AGENT_API_KEY`：远端 Agent API Key。只写入本地或部署环境，不要提交。
- `AGENT_MODEL`：默认模型名。
- `AGENT_MODEL_ALLOWLIST`：允许前端切换的模型白名单；为空时只使用默认模型。
- `AGENT_TTS_VOICE` / `AGENT_TTS_SPEED`：聊天回复默认音色和语速。
- `AGENT_ASR_MODEL`：聊天使用的 ASR 模型；留空使用默认 ASR。
- `CHAT_BARGE_IN` / `CHAT_BARGE_IN_MIN_CHARS`：级联模式默认是否允许说话打断，以及触发打断的最少有效字符数。
- `CHAT_MIN_SPEECH_CHARS` / `CHAT_MIN_SPEECH_MS`：过滤环境噪声和过短误触发的有效发言门槛。

### 原生端到端语音

- `QWEN_OMNI_ENABLED`：是否在 Chat 页暴露原生语音模式。
- `QWEN_OMNI_ENDPOINT`：百炼 Workspace 专属 WebSocket 地址，不含 `model` 查询参数。
- `QWEN_OMNI_API_KEY`：百炼 API Key，只写本地或部署环境。
- `QWEN_OMNI_MODEL` / `QWEN_OMNI_MODEL_ALLOWLIST`：默认模型和前端可选白名单。
- `QWEN_OMNI_VOICES` / `QWEN_OMNI_DEFAULT_VOICE`：前端可选音色及默认音色。
- `QWEN_OMNI_CONCURRENCY`：云端原生语音会话并发上限，独立于本地 GPU 信号量。
- `QWEN_OMNI_TURN_DETECTION`：`semantic_vad` 或 `server_vad`。
- `QWEN_OMNI_VAD_THRESHOLD` / `QWEN_OMNI_SILENCE_MS`：服务端 VAD 灵敏度与静音端点时长。
- `QWEN_OMNI_SILENCE_OPTIONS`：允许前端选择的静音端点档位，默认 `[800,1500,2000]`；未命中列表的请求回退 `QWEN_OMNI_SILENCE_MS`。

原生模式必须持续上传音频，前端会自动关闭本地能量 VAD，避免丢失停顿、笑声等副语言信息。输入为 16 kHz PCM，输出为 24 kHz PCM。能力目录同时下发 Provider 推荐值：原生模式默认 `barge-in=true`、自然采音（AEC 开、降噪/AGC 关）；级联模式默认使用本地 VAD 和嘈杂环境采音。切换模式时前端会完整重置这些值，避免状态串扰。

配置完整并启用后，前端 Chat 页会默认优先选择原生语音模式；未配置时只展示级联模式。默认 2000 ms 用于避免句中自然停顿被服务端 VAD 过早切开，前端可选择快速 800 ms、均衡 1500 ms 或长停顿 2000 ms。

## 部署与安全

- `NODE_NAME`：节点名，会随响应返回给前端和监控，用于多节点观察。
- `CORS_ORIGINS`：同源反代时通常无需放开；直连后端时再按需配置。
- `ASR_CONCURRENCY` / `TTS_CONCURRENCY`：GPU 推理并发上限，单卡建议从 1-2 起步。
- `GPU_ACQUIRE_TIMEOUT`：等待推理槽位超时时间，超时返回 429。

请勿提交以下内容：

- API Key、Token、密码、云账号凭据；
- SSH 私钥、证书私钥；
- 真实公网/内网 IP 和运维账号；
- 模型权重、私有音频、用户语音数据。
