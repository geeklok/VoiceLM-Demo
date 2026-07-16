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

## TTS

- `TTS_ENGINE`：`stub` 或 `cosyvoice`。
- `COSYVOICE_REPO_DIR`：CosyVoice 仓库根目录。
- `COSYVOICE_MODEL`：CosyVoice2 模型 ID 或本地模型目录。
- `COSYVOICE_DEFAULT_VOICE`：默认音色名，必须存在于 `COSYVOICE_VOICES`。
- `COSYVOICE_VOICES`：JSON，格式为 `音色名 -> {audio_path, prompt_text}`。参考音频建议为 16 kHz WAV，并确保 `prompt_text` 与音频内容一致。
- `TTS_SENTENCE_STREAM`：长文本是否按句流式合成。

示例：

```bash
COSYVOICE_DEFAULT_VOICE=中文女
COSYVOICE_VOICES='{"中文女":{"audio_path":"/voices/default.wav","prompt_text":"这里填女声参考音频准确的文字内容"},"中文男":{"audio_path":"/voices/male.wav","prompt_text":"这里填男声参考音频准确的文字内容"}}'
```

## Chat / 远端 Agent

- `CHAT_ENABLED`：是否启用语音聊天。
- `AGENT_ENDPOINT`：OpenAI-compatible API base URL，可带或不带 `/v1`。
- `AGENT_API_KEY`：远端 Agent API Key。只写入本地或部署环境，不要提交。
- `AGENT_MODEL`：默认模型名。
- `AGENT_MODEL_ALLOWLIST`：允许前端切换的模型白名单；为空时只使用默认模型。
- `AGENT_TTS_VOICE` / `AGENT_TTS_SPEED`：聊天回复默认音色和语速。
- `AGENT_ASR_MODEL`：聊天使用的 ASR 模型；留空使用默认 ASR。

### 原生端到端语音

- `QWEN_OMNI_ENABLED`：是否在 Chat 页暴露原生语音模式。
- `QWEN_OMNI_ENDPOINT`：百炼 Workspace 专属 WebSocket 地址，不含 `model` 查询参数。
- `QWEN_OMNI_API_KEY`：百炼 API Key，只写本地或部署环境。
- `QWEN_OMNI_MODEL` / `QWEN_OMNI_MODEL_ALLOWLIST`：默认模型和前端可选白名单。
- `QWEN_OMNI_VOICES` / `QWEN_OMNI_DEFAULT_VOICE`：前端可选音色及默认音色。
- `QWEN_OMNI_CONCURRENCY`：云端原生语音会话并发上限，独立于本地 GPU 信号量。
- `QWEN_OMNI_TURN_DETECTION`：`semantic_vad` 或 `server_vad`。
- `QWEN_OMNI_VAD_THRESHOLD` / `QWEN_OMNI_SILENCE_MS`：服务端 VAD 灵敏度与静音端点时长。

原生模式必须持续上传音频，前端会自动关闭本地能量 VAD，避免丢失停顿、笑声等副语言信息。输入为 16 kHz PCM，输出为 24 kHz PCM。

配置完整并启用后，前端 Chat 页会默认优先选择原生语音模式；未配置时只展示级联模式。`QWEN_OMNI_SILENCE_MS` 默认 2000 ms，主要用于避免句中自然停顿被服务端 VAD 过早切开。

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
