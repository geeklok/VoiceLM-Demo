> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 语音大模型后端服务（ASR + TTS）技术方案与实施计划

> 状态：已批准并实施 | 创建：2026-06-26 | 更新：2026-07-18 | 技术栈：Python(FastAPI) + React/TS + 阿里系语音大模型（FunASR / CosyVoice / Qwen-Omni-Realtime）
>
> **进度（2026-06-27）**：Phase 1 MVP + M4 公网部署 + **Phase 2（单机工程化 + 水平扩容/容灾）已完成并上线**。线上入口 **https://your-domain.example.com**（阿里云 GPU ECS，Tesla T4 16GB），文件式 + 流式 ASR/TTS 均可用，HTTPS 自签证书 + 公网 IP。**双机集群已上线**：入口机 <PUBLIC_HOST>（内网 <NODE1_PRIVATE_IP>，Web+LB+Backend / node1）+ 扩展机 <NODE2_PUBLIC_HOST>（内网 <NODE2_PRIVATE_IP>，Backend-only / node2），nginx `least_conn` + `proxy_next_upstream` 实现真扩容（LB 分流实测）+ 整机高可用（优雅停 / SIGKILL 零中断）。**Phase 3 进行中：Step 1（QoS 埋点 + Prometheus/Grafana 可观测性）已上线**——两机 backend 暴露 `/metrics`，监控栈跑在入口机（端口仅绑 127.0.0.1，SSH 端口转发访问），Grafana 看板 `asr-tts-qos` 已出图，baseline 已建（ASR RTF ~0.029）；B 线 vLLM 推理优化待做。详见 §9 里程碑状态、《公网部署Runbook-MVP》《HTTPS配置Runbook-自签证书》《水平扩容Runbook-单机LB骨架》《可观测性Runbook-QoS监控栈》。
>
> **增量（2026-07-01，热词能力）**：ASR 引擎矩阵调整——新增 `funasr-seaco`（SeacoParaformer，**唯一真支持热词偏置**），下线 `funasr-paraformer-zh`（seaco 是其功能超集，不给热词时退化同一基座、实测逐字一致）。真流式 `funasr-streaming` 升级为**动态定稿**：句末带热词走 seaco 重解码、不带走 SenseVoice（保多语种+情感 emoji），两引擎均常驻、零额外显存。引入能力标志 `supports_hotwords` 经 `/api/v1/models` 下发，前端据此**禁用不支持模型的热词框**（不硬编码模型名）。node1 + node2 均已上线并 ws 实测通过（node2 用 overlay 增量构建规避 Docker Hub 不可达）。详见《实时录音转写-真流式2pass方案》热词增强章节、《水平扩容Runbook-单机LB骨架》§3.6。

---

## 1. 项目概述（Summary）

搭建一套提供 **ASR（语音识别）** 和 **TTS（语音合成）** 能力的语音大模型后端服务，配套一个 Web 交互页面。能力底座全部采用**阿里系开源模型自托管**方案：

- **ASR**：FunASR（阿里 DAMO 开源，MIT 协议）。MVP 用 Paraformer/SenseVoice（非自回归、ONNX 推理、低延迟、成熟稳定）；Phase 3 引入 Fun-ASR-Nano（LLM-based，支持 vLLM PagedAttention）作为高精度可选模型。
- **TTS**：CosyVoice2（阿里通义开源，0.5B，Qwen2.5 backbone）。原生支持流式合成（首包 ~150ms）、vLLM、TensorRT、fp16。

按三阶段迭代推进：

| 阶段 | 目标 | 核心交付 |
|---|---|---|
| **Phase 1 — MVP** | 打通最小可用链路 | 文件式 + 流式 两种交互；分层后端架构（含音频预处理层）；Web 页面 |
| **Phase 2 — 扩展/扩容/容灾** | 工程化与稳定性 | 多模型选择、实例预热、负载自动扩缩、健康检查、容灾降级 |
| **Phase 3 — QoS 与性能优化** | 指标驱动调优 | QoS 指标体系；PagedAttention/KV Cache/连续批处理等大模型推理优化 |

**部署目标**：阿里云 GPU 实例（ECS GN 系列），公网可访问。

---

## 2. 关键决策与约束（Assumptions & Decisions）

基于与用户的澄清确认：

1. **部署形态**：自托管开源模型（非托管 API）。理由：Phase 3 的 PagedAttention/KV Cache 等推理引擎级优化只有自托管才能落地。
2. **交互形态**：MVP 同时支持「文件上传/一次性」与「实时流式」两种模式。
3. **技术栈**：后端 Python + FastAPI（与 FunASR/CosyVoice 同为 Python 生态，集成最自然）；前端 React + TypeScript + Vite。
4. **模型来源**：ModelScope（阿里）下载权重。
5. **协议**：HTTP/REST 用于文件式与管理接口；WebSocket 用于流式 ASR 与流式 TTS。

**待用户配合事项（届时会直接 @ 你）**：
- 阿里云 GPU ECS 实例开通。原方案建议 A10 24GB；**实测 Tesla T4 16GB 即可同时跑 SenseVoiceSmall + CosyVoice2-0.5B**（显存占用约 3.8/15GB，富余充足），MVP 成本可更低。
- ~~公网域名 + ICP 备案~~ → **已用自签证书 + 公网 IP 直连绕开**。大陆 IP 直连无需备案即可对外提供 80/443。仅当需要消除浏览器证书警告 / 用域名访问时，才需走 ICP 备案 + 受信任证书（1–3 周）。
- 对象存储 OSS（存音频文件，可选）。**当前仍未启用，用本地磁盘 / 内存即可。**
- HTTPS 证书（浏览器麦克风权限要求 HTTPS）。**已用自签证书满足**——M4 上 HTTPS 后，麦克风 / 流式 ASR / 流式 TTS 已全部解锁。

---

## 3. 现状分析（Current State Analysis）

- 工作目录 `<repo-root>` 当前为**空仓库**，全新项目，无历史代码约束。
- 无现有约定/依赖，可按最佳实践自由设计目录结构。
- 本地为 macOS（开发机），GPU 推理需在阿里云 Linux 实例上运行；本地以 CPU 模式或连接远程推理服务进行开发联调。

---

## 4. 整体架构设计

### 4.1 后端分层（基于语音大模型业务特性）

针对用户强调的"层级设计"，特别是**输入音频参数与模型期望参数不一致时的预处理**，后端分为五层：

```
┌─────────────────────────────────────────────────────────┐
│  L1 接入层 (API Gateway Layer)                              │
│   - FastAPI: REST(文件式) + WebSocket(流式)                  │
│   - 鉴权、限流、请求校验、CORS                                 │
├─────────────────────────────────────────────────────────┤
│  L2 编排层 (Orchestration Layer)                            │
│   - 请求路由: ASR / TTS                                      │
│   - 模型选择(Phase 2)、会话管理、流式分块调度                    │
├─────────────────────────────────────────────────────────┤
│  L3 音频预处理层 (Audio Preprocessing Layer)  ★业务核心        │
│   - 解码: 任意格式(mp3/m4a/webm/wav...) → PCM (ffmpeg)        │
│   - 重采样: 任意采样率 → 模型期望 16kHz                         │
│   - 声道处理: 立体声 → 单声道                                  │
│   - 位深/编码统一: → 16bit PCM float32                        │
│   - VAD 切分、音量归一化、(可选)降噪                            │
│   - 参数协商: 校验输入参数 vs 模型能力，自动适配或拒绝             │
├─────────────────────────────────────────────────────────┤
│  L4 推理服务层 (Inference Service Layer)                     │
│   - ASR Engine: FunASR (Paraformer/SenseVoice/Fun-ASR-Nano)│
│   - TTS Engine: CosyVoice2                                  │
│   - 实例池管理、预热、批处理(Phase 2/3)                         │
├─────────────────────────────────────────────────────────┤
│  L5 后处理层 (Post-processing Layer)                         │
│   - ASR: 标点恢复、ITN(逆文本归一化)、热词、时间戳              │
│   - TTS: 音频封装(wav/mp3)、采样率回转、流式分包                │
└─────────────────────────────────────────────────────────┘
```

**为什么音频预处理层是业务核心**：语音大模型对输入有严格契约——FunASR 模型期望 16kHz/单声道/16bit PCM，CosyVoice 的 prompt 音频期望 16kHz。而真实用户输入千差万别（手机录音 44.1kHz 立体声 m4a、浏览器 MediaRecorder 的 48kHz webm/opus 等）。预处理层负责把"用户世界的音频"翻译成"模型世界的音频"，这是服务正确性的关键，也是 MVP 必须独立成层的原因。

### 4.2 推理与服务进程拓扑

```
                          ┌──────────────┐
   Web (React)  ──HTTPS──▶ │  Nginx       │ (反代 + TLS + 静态资源)
                          └──────┬───────┘
                                 │
                   ┌─────────────▼──────────────┐
                   │  FastAPI App (asr-backend)  │  ← L1~L3, L5
                   │  REST + WebSocket           │
                   └──────┬───────────────┬──────┘
                          │ gRPC/HTTP     │ Python in-proc 或 子进程
              ┌───────────▼───┐    ┌──────▼───────────┐
              │ ASR Inference │    │ TTS Inference     │  ← L4
              │ (FunASR)      │    │ (CosyVoice2)      │
              │  GPU          │    │  GPU              │
              └───────────────┘    └──────────────────┘
```

- **MVP**：推理引擎与 FastAPI 同进程加载（简单、够用），通过单例 + 异步线程池避免阻塞事件循环。
- **Phase 2**：推理层拆为独立服务/进程（解耦扩缩容），FastAPI 通过内部 RPC 调用，引入实例池与负载均衡。

### 4.3 当前交互与稳定性闭环（2026-07-18）

```text
ASR file: 分块限长上传 -> 线程池 ffmpeg -> 模型契约缓存 -> ASR
ASR live: WS start -> ready -> 浏览器开麦 + 200ms pre-roll -> partial/final

TTS file: 文本清洗/TN -> 严格模型与音色校验 -> 一次性合成
TTS live: 有界队列 -> CosyVoice 同步生成线程 -> WS -> 客户端可取消

Chat: GET /api/v1/chat/models -> Provider 推荐参数
      -> cascade (ASR -> Agent -> TTS) 或 native (Qwen Realtime)
      -> 停止回复/说话打断 -> 上游取消 + 停播 -> 回到 listening
```

- 文件 ASR 的同步 `ffmpeg` 已移入线程池；相同模型输入契约的 fallback 复用预处理结果，避免阻塞事件循环和重复解码。
- 显式模型名和 TTS 音色严格校验，未知值返回 400 / `bad_request`，不再静默回退。
- CosyVoice 流式桥接使用有界队列和协作取消。生成器关闭后等待同步生产线程退出，再释放 TTS GPU slot。
- ASR、TTS、Chat WebSocket 都有前端建连超时和异常关闭恢复；ASR 只在服务端 `ready` 后打开麦克风。
- Chat Provider 能力目录下发 `barge-in`、本地 VAD、采音档位和端点静音推荐值。模式切换时完整重置，原生失败时可切回级联。
- 级联 Chat 可在 THINKING 阶段停止 Agent；被打断时只把已完整下发的句子写入历史。原生 Chat 向上游发送 `response.cancel`，避免只停本地播放而继续计费。

---

## 5. Phase 1 — MVP 详细设计与实现

### 5.1 目标
打通 `Web → 后端 → 模型 → 返回` 的完整闭环，支持中文/英文 ASR 与中文 TTS，文件式 + 流式两种交互。

### 5.2 后端目录结构（新建）

```
asr/
├── backend/
│   ├── pyproject.toml                # 依赖管理(uv/poetry)
│   ├── app/
│   │   ├── main.py                   # FastAPI 入口
│   │   ├── config.py                 # 配置(pydantic-settings)
│   │   ├── api/
│   │   │   ├── routes_asr.py         # POST /api/v1/asr  + WS /ws/asr
│   │   │   ├── routes_tts.py         # POST /api/v1/tts  + WS /ws/tts
│   │   │   └── routes_health.py      # GET /healthz /readyz /models
│   │   ├── orchestration/
│   │   │   └── dispatcher.py         # L2 编排
│   │   ├── audio/                    # ★ L3 预处理层
│   │   │   ├── decoder.py            # ffmpeg 解码/转码
│   │   │   ├── resampler.py          # 重采样到 16k
│   │   │   ├── channel.py            # 声道转换
│   │   │   ├── vad.py                # VAD 切分(FSMN)
│   │   │   └── pipeline.py           # 预处理编排 + 参数协商
│   │   ├── engines/                  # L4 推理封装
│   │   │   ├── base.py               # ASREngine / TTSEngine 抽象基类
│   │   │   ├── registry.py           # 引擎注册表 + 预热(MVP 已落地, 原计划 Phase 2)
│   │   │   ├── funasr_engine.py      # FunASR 封装
│   │   │   ├── cosyvoice_engine.py   # CosyVoice2 封装
│   │   │   └── stub_engine.py        # 本地无 GPU 的占位引擎(开发联调用)
│   │   ├── postprocess/              # L5
│   │   │   └── audio_encode.py       # PCM → wav/mp3 (标点/ITN 委托模型自带, 未单列 text_post)
│   │   ├── schemas/                  # pydantic 请求/响应模型
│   │   └── utils/                    # 日志、异常、计时
│   └── tests/
└── frontend/                         # 见 5.6
```

### 5.3 抽象层设计（为 Phase 2 多模型预留）

[base.py](../../backend/app/engines/base.py) 定义引擎契约，这是 Phase 2 多模型的扩展点：

```python
class ASREngine(ABC):
    name: str
    expected_sample_rate: int   # 模型期望采样率(供预处理层协商)
    expected_channels: int
    @abstractmethod
    async def transcribe(self, pcm: np.ndarray, **opts) -> ASRResult: ...
    @abstractmethod
    async def transcribe_stream(self, chunks) -> AsyncIterator[ASRPartial]: ...
    @abstractmethod
    async def warmup(self) -> None: ...     # Phase 2 预热

class TTSEngine(ABC):
    name: str
    output_sample_rate: int
    @abstractmethod
    async def synthesize(self, text: str, **opts) -> bytes: ...
    @abstractmethod
    async def synthesize_stream(self, text) -> AsyncIterator[bytes]: ...
    @abstractmethod
    async def warmup(self) -> None: ...
```

### 5.4 API 契约（MVP）

**ASR — 文件式**
```
POST /api/v1/asr
Content-Type: multipart/form-data
  file: <audio>            # 任意格式/采样率(预处理层负责适配)
  language: zh|en|auto     # 默认 auto
  hotwords: ["专有名词"]    # 可选
→ 200 { "text": "...", "duration_ms": 1234, "segments": [...], "rtf": 0.05 }
```

**ASR — 流式（WebSocket）**
```
WS /ws/asr
  client → {"type":"start","sample_rate":16000,"mode":"2pass"}
  server → {"type":"ready","node":"...","model":"..."}  # 收到后再开麦
  client → <binary PCM chunk> ...
  client → {"type":"end"}
  server → {"type":"partial","text":"..."}      # 实时预览
  server → {"type":"final","text":"...","segment_id":1}
```
（采用 FunASR 2pass 模式：流式低延迟出 partial，离线高精度出 final）

**TTS — 文件式**
```
POST /api/v1/tts
  { "text":"你好世界", "voice":"中文女", "speed":1.0, "format":"wav" }
→ 200  audio/wav (binary)
```

**TTS — 流式（WebSocket，边合成边播）**
```
WS /ws/tts
  client → {"type":"synthesize","text":"...","voice":"..."}
  server → <binary audio chunk> ...      # 首包 ~150ms
  server → {"type":"done"}
```

**管理/健康**
```
GET /healthz   → 进程存活
GET /readyz    → 模型已加载就绪
GET /api/v1/models → 可用模型列表(Phase 2 起有多项)
```

### 5.5 音频预处理层实现要点（★MVP 重点）

[pipeline.py](../../backend/app/audio/pipeline.py) 流程：
1. **嗅探格式**：读取文件头/MIME，识别容器与编码。
2. **解码转码**：用 `ffmpeg`（通过 `ffmpeg-python` 或 subprocess）统一解码为 PCM。
3. **重采样**：`librosa`/`soxr` 将任意采样率转为模型 `expected_sample_rate`（16k）。
4. **声道归一**：立体声 downmix 为单声道。
5. **格式统一**：转 float32 / 16bit PCM。
6. **VAD**（流式必需，文件式可选）：FunASR FSMN-VAD 切分有效语音段。
7. **参数协商**：对比输入参数与 `engine.expected_*`，能适配则自动转换，不能则返回明确 4xx 错误。

流式额外处理：浏览器 `MediaRecorder` 通常产出 48kHz webm/opus，需在浏览器端用 `AudioContext` 降采样为 16kHz PCM 后再传，或后端实时转码（MVP 采用前端降采样，减轻后端压力）。

### 5.6 前端（React + TS + Vite）

实际落地结构（音频逻辑收敛为 `audio/` 下的纯 TS 模块，未拆 components 组件目录）：

```
frontend/
├── src/
│   ├── pages/
│   │   ├── AsrPage.tsx        # 上传文件 / 录音 → 显示识别文本
│   │   └── TtsPage.tsx        # 输入文本 → 播放/下载合成音频
│   ├── audio/
│   │   ├── recorder.ts        # getUserMedia + 降采样到 16k PCM
│   │   └── player.ts          # 流式 PCM 播放
│   ├── api/client.ts          # REST + WebSocket 封装(wsBase 按协议自动 ws/wss)
│   ├── App.tsx
│   ├── main.tsx
│   └── styles.css
├── nginx.conf                 # 生产: 静态资源 + 反代 + (M4)443 TLS
├── Dockerfile                 # 纯 nginx COPY dist
├── package.json
└── vite.config.ts
```
功能：ASR 页支持文件上传 + 实时录音（流式）；TTS 页支持文本输入、音色/语速选择、在线播放与下载、流式播放。

### 5.7 Phase 1 验收标准
- [ ] 上传一段 mp3/m4a/wav（任意采样率）→ 返回正确中文/英文文本。
- [ ] 浏览器录音实时转写，partial 文本流式刷新，end 后出 final。
- [ ] 输入中文文本 → 返回可播放 wav；流式模式首包延迟可感知地快。
- [ ] 预处理层单测：48kHz 立体声 m4a 正确转为 16kHz 单声道 PCM。
- [ ] `/readyz` 在模型加载完成后返回就绪。

---

## 6. Phase 2 — 扩展 / 扩容 / 容灾

### 6.1 多模型选择
- 引擎注册表 `EngineRegistry`：启动时按配置加载多个 ASR/TTS 引擎（如 SenseVoice-Small 低延迟版 + Fun-ASR-Nano 高精度版；CosyVoice2 多音色）。
- API 增加 `model` 参数；`GET /api/v1/models` 返回能力矩阵（语言、采样率、延迟档位、**是否支持热词 `supports_hotwords`**）。
- 路由策略：按请求显式指定，或按 SLA（低延迟/高精度）自动选择。

**当前线上 ASR 引擎矩阵（2026-07-01）**：

| 引擎 name | 底座 | supports_hotwords | 特点 / 用途 |
|---|---|---|---|
| `funasr-sensevoice` | SenseVoiceSmall | False | 多语种 + 情感/事件富文本（😡😊 emoji），文件式与流式默认定稿 |
| `funasr-seaco` | SeacoParaformer | **True** | **唯一真支持热词偏置**（bias encoder）+ 标点，文件式与流式带热词时的定稿 |
| `funasr-streaming` | paraformer-online + 上二者 | 条件性（注入 seaco 才 True） | 真流式 2pass，**默认引擎**；句末动态定稿（带热词→seaco / 否则→SenseVoice） |

- `funasr-paraformer-zh` 已下线（seaco 是其功能超集，不给热词时退化同一基座、实测逐字一致，保留冗余）。
- **能力标志驱动前端**：`supports_hotwords` 经 `/api/v1/models` 下发，前端 [AsrPage.tsx](../../frontend/src/pages/AsrPage.tsx) 按当前选中模型禁用/启用热词输入框——**不硬编码模型名**（streaming 是否支持热词取决于部署有无配 seaco）。详见《实时录音转写-真流式2pass方案》热词增强章节。

### 6.2 实例预热（Warmup）
- 服务 `readyz` 之前对每个引擎跑一次 dummy 推理，触发 CUDA kernel 编译、权重换入显存、JIT/TRT 图构建，避免首请求长尾。
- CosyVoice 的 `load_jit/load_trt` 图编译在预热阶段完成。

### 6.3 多实例 / 负载扩缩容
- **推理层独立化**：将 ASR/TTS 引擎拆为独立服务进程（FastAPI app 通过内部 HTTP/gRPC 调用），便于独立扩缩。
- **实例池 + 队列**：每张 GPU 跑 N 个 worker，前置请求队列；队列积压/GPU 利用率超阈值时水平扩容（Docker Compose 多副本 → 后续 K8s HPA）。
- **负载均衡**：Nginx / 内部 LB 按最少连接分发。
- **并发控制**：信号量限制单实例并发，超限请求排队或快速失败（返回 429 + Retry-After）。

### 6.4 容灾与降级
- **健康检查 + 自动摘除**：实例 `/healthz` 失败自动从 LB 摘除。
- **模型降级链**：高精度模型超时/OOM → 自动降级到低延迟模型。
- **熔断**：连续失败触发熔断，快速失败避免雪崩。
- **优雅退出**：收到 SIGTERM 先停止接新请求，处理完存量再退出。

### 6.5 容器化与配置
- 每个服务一个 Dockerfile（CUDA base 镜像）。
- `docker-compose.yml` 编排 nginx + backend + asr-infer + tts-infer。
- 配置外置（环境变量 / `.env`），模型路径、并发数、扩缩阈值可调。

### 6.6 Phase 2 验收标准
- [ ] 同时加载 ≥2 个 ASR 模型，API 可指定切换。
- [ ] 服务就绪前完成预热，首请求无明显冷启动长尾。
- [ ] 压测下队列积压触发多实例，QPS 线性提升。
- [ ] 杀掉一个推理实例，LB 自动摘除，请求不中断。

---

## 7. Phase 3 — QoS 指标体系与性能优化

### 7.1 ASR / TTS 核心 QoS 指标

**ASR 关键指标**

| 指标 | 含义 | 目标方向 |
|---|---|---|
| RTF (Real-Time Factor) | 处理时长 / 音频时长 | 越小越好（<0.1 优秀） |
| 首字延迟 (First-Token Latency) | 流式下首个字返回耗时 | 越低越好 |
| 端到端延迟 | 说完到出最终结果 | 越低越好 |
| CER / WER | 字/词错误率 | 越低越好 |
| 吞吐 (并发路数) | 单卡可支撑并发流数 | 越高越好 |
| VAD 端点延迟 | 语音结束到判定结束 | 平衡延迟与截断 |

**TTS 关键指标**

| 指标 | 含义 | 目标方向 |
|---|---|---|
| 首包延迟 (TTFB) | 请求到首个音频包 | 越低越好（CosyVoice2 ~150ms） |
| RTF | 合成时长 / 音频时长 | <1 才能实时，越小越好 |
| 流式平滑度 | 音频包是否断续 | 无卡顿 |
| MOS | 自然度主观评分 | 越高越好 |
| 音色相似度 | zero-shot 克隆相似度 | 越高越好 |
| 并发吞吐 | 单卡并发合成路数 | 越高越好 |

### 7.2 针对性优化手段

**大模型推理优化（用户明确要求，依赖 LLM-based 模型）**
- **PagedAttention**：ASR 用 Fun-ASR-Nano + vLLM 引擎（`AutoModelVLLM`）；TTS 用 CosyVoice2 `load_vllm=True`。两者 LLM 解码部分均跑在 vLLM，由 PagedAttention 管理 KV 显存，降低碎片、提升并发。
- **KV Cache**：vLLM 自动复用解码缓存；流式场景下增量解码避免重复计算（FunASR 流式累积音频 + CosyVoice 块级因果 flow matching）。
- **连续批处理 (Continuous Batching)**：vLLM 动态拼批，多并发请求共享 GPU，吞吐显著提升。
- **张量并行**：多卡时 `tensor_parallel_size>1`，支撑更大模型/更高并发。

**精度/吞吐优化**
- **量化**：fp16 / int8；CosyVoice 支持 fp16；ASR ONNX 模型支持量化版。
- **TensorRT**：CosyVoice `load_trt=True` 编译 flow/hift 模块加速。
- **ONNX Runtime + 动态批处理**：Paraformer/SenseVoice 非 LLM 模型走 ONNX，开启 batch。

**延迟优化**
- 流式分块大小调优（chunk_size、chunk_interval）平衡首字延迟与精度。
- VAD 端点参数调优。
- 预处理层零拷贝、流水线并行（解码与推理重叠）。
- **分句流式合成（已落地，node1 灰度）**：后端按句末标点（中英文 `。！？!?；;` + 换行）把长文本分句，在**同一个 TTS 信号量内逐句**调 `synthesize_stream`，让**第一短句**快速出首块，从而把 TTFB 绑定到**首句长度**而非全文长度。纯应用层逻辑、零依赖、零模型改动、引擎无关，默认关（`tts_sentence_stream`，超长句按次标点/硬截断到 `tts_max_sentence_chars`=60）。**适用边界**：对多句长文本（含句末标点的段落）有效——首句短、先出声；对单长句无效（首包惩罚来自 LLM 段处理整句 prompt，分句切不进句内）。实测收益见《可观测性Runbook-QoS监控栈》§6.5/§6.5.2：多句语料 node1 vs node2（基线）TTFB p50 约 -68%。这是在 T4 上不动 LLM 段（vLLM 高风险，见 §10）也能拿到的低风险 TTFB 优化。
- **句间间隔修复 / 最小段长贪心合并（已落地，node1 灰度）**：分句流式的副作用——句长差大时句间播放有明显间隔。根因是单 GPU 串行下每句各跑一遍 LLM prefill（~1.5s），前端播放器无 jitter buffer，首句音频盖不住下句 prefill 即欠载留空档。修复：`split_sentences` 加 `min_chars`（`tts_min_sentence_chars`，默认 0），分句后把过短段**向后贪心合并到 `≥min_chars`**（不超 `max_chars`），保证首段音频够长盖住下段 prefill。用一点 TTFB 换播放平滑；`min_chars=0` 不合并=保持现状。node1 设 `TTS_MIN_SENTENCE_CHARS=5`（经 20→8→5 微调后定值），node2 维持基线对照。详见《可观测性Runbook-QoS监控栈》§6.5.3。

**文本前处理（输入质量）**
- **TTS markdown 剥离（已落地，双机上线）**：用户反馈输入 markdown（`**`/`#`/链接/表格等）时 TTS 读错。node1 容器实锤确认**非模型能力问题**——CosyVoice `text_normalize` 只做 TN（数字读法/标点规整），**不剥离 markdown 语法**，于是 `#*_`` ``、`[文字](url)`、竖线、裸 URL 全被逐字念出或打乱韵律。修复：新建 [text_clean.py](../../backend/app/utils/text_clean.py) `strip_markdown`，在送引擎前把 markdown 转纯文本（链接留文字丢 URL、删图片/裸 URL、去强调/标题/列表/引用标记、代码围栏保留内部代码、表格竖线转停顿）。接在 [dispatcher.py](../../backend/app/orchestration/dispatcher.py) 的 `tts_file` 与 `tts_stream`（`split_sentences` 之前），引擎无关、零依赖、无条件生效（纯文本无副作用故不设开关）。node1 常规 rebuild、node2 overlay 增量构建，双机真实 `/api/v1/tts` 带 markdown 返回 200 audio/wav 无回归。详见《分句流式TTS-TTFB优化方案》附录。

**容量优化**
- KV cache 显存占用调参（`gpu_memory_utilization`）。
- 模型实例与并发数按 QoS SLA 配置档位。

### 7.3 可观测性（指标驱动的前提）
- 接入 Prometheus 指标埋点（RTF、TTFB、队列长度、GPU 利用率、错误率），Grafana 看板。
- 每个请求记录全链路耗时分解（预处理 / 推理 / 后处理），定位瓶颈。

### 7.4 Phase 3 验收标准
- [ ] QoS 指标全部接入 Grafana 看板，可实时观测。
- [ ] 开启 vLLM PagedAttention 后，相同 GPU 并发吞吐相比 baseline 提升（给出对比数据快照）。
- [ ] TTS 首包延迟达到 ~150ms 量级。
- [ ] 提供一份优化前后的 QoS 对比报告（含数据快照日期）。

---

## 8. 部署方案（公网）

> **已落地形态（M4，2026-06-27）**：阿里云 GPU ECS（**Tesla T4 16GB**，Ubuntu 22.04）+ Docker Compose（nginx web 容器反代 + backend GPU 容器）。HTTPS 用**自签证书 + 公网 IP**（`https://your-domain.example.com`），nginx 80→443 跳转。详见《公网部署Runbook-MVP》《HTTPS配置Runbook-自签证书》。下列为原始规划，标注与实际差异：

1. **GPU 实例**：原计划 A10 24GB；**实测 T4 16GB 足够**（MVP 单卡共跑 ASR+TTS，显存富余）。Ubuntu 22.04 + CUDA 12.1 镜像。
2. **模型下载**：从 ModelScope 拉取权重。MVP 实际只用 **SenseVoiceSmall + FSMN-VAD + CosyVoice2-0.5B**；Paraformer / Fun-ASR-Nano 留待 Phase 2/3。
3. **反向代理 + TLS**：Nginx。~~Let's Encrypt~~ → MVP 用**自签证书**（0 成本、免备案、当天上线，代价是浏览器一次性警告）。需消除警告时再换受信任证书。
4. **域名与备案**：~~大陆节点需 ICP 备案~~ → **自签 + 公网 IP 直连已绕开备案**，无需域名即上线。仅在要域名访问时才走备案。
5. **进程守护**：Docker Compose（`restart: unless-stopped`）+ healthcheck；日志/监控接入留待 Phase 3。
6. **CI/CD（可选）**：当前为本地 `npm run build` 产出 dist + rsync 到实例、实例上 `docker compose build`，未接 CI。

> 部署阶段需要你配合开通 GPU 实例、放行安全组端口（80/443）等时，我会直接 @ 你；涉及花钱的云资源我只提示、不代购。

---

## 9. 实施里程碑（按迭代节奏）

| 里程碑 | 内容 | 产出 | 状态 |
|---|---|---|---|
| **M0 脚手架** | 仓库结构、依赖、FastAPI + Vite 骨架、健康检查 | 可运行空服务 | ✅ 完成 |
| **M1 ASR 文件式** | 预处理层 + FunASR 文件转写 + 前端上传页 | 上传音频出文本 | ✅ 完成 |
| **M2 TTS 文件式** | CosyVoice2 合成 + 前端 TTS 页 | 文本出音频 | ✅ 完成 |
| **M3 流式** | WS 流式 ASR(2pass) + 流式 TTS + 录音组件 | 实时交互闭环 | ✅ 完成 |
| **M4 公网部署** | 阿里云 GPU 部署 + HTTPS（自签 + IP，**无域名**） | 公网可访问 MVP（https://your-domain.example.com） | ✅ 完成 |
| **M5 Phase 2** | 多模型 + 预热 + 实例池 + 容灾 + 水平扩容（单机多副本 + 双机集群） | 可扩缩容服务（双机集群上线） | ✅ 完成 |
| **M6 Phase 3** | vLLM/PagedAttention + QoS 看板 + 调优报告 | 优化后服务 + 报告 | 🟡 进行中（Step 1 QoS 埋点 + Prometheus/Grafana 已上线；**B 线分句流式 TTFB 优化已落地**：node1 灰度，多句语料 TTFB p50 ↓~68%，node2 维持基线对照，见 runbook §6.5/§6.5.2；**热词增强已落地并双机上线**：seaco 接入 + streaming 动态定稿 + `supports_hotwords` 能力下发，见《实时录音转写-真流式2pass方案》热词增强章节；**语音聊天（speech-to-speech）已落地上线**（**双机 LB**）：ASR→远端 Agent(OpenAI 兼容 SSE)→流式 TTS 对话环，2026-07-04 增 barge-in 说话打断（`chat_barge_in` 默认关，前端 start 帧 `barge_in` 逐连接可选，浏览器 AEC + ASR 文字判据）+ **噪声误触发修复（前端能量 VAD 门控默认开 + 后端有效发言门控 `chat_min_speech_chars=2`/`chat_min_speech_ms=300`，双机上线）**；2026-07-05 再补 **咳嗽等非语言声修复（SenseVoice 事件标签层 `funasr_drop_nonspeech_events` 默认开，`<|Cough|>`/`<|Sneeze|>`/`<|BGM|>` 等命中即判噪声丢弃，node1 已上线、node2 待同步）**；node2 经实测能出网连 Agent，`/ws/chat` 已 node1+node2 双机 LB（公网 8 轮实测两机交替、每轮跑通），见《语音聊天-语音到语音对话方案》§7 更正/§12/§13/§14；vLLM 推理优化经评估为高风险（T4 不支持 bf16 + torch 2.3→2.7 大版本升级）暂缓，见 runbook §6.3；**2026-07-05 TTS 加速两轮评估**：① fp16 双机灰度实测为负面（n=20，5 语料中 4 条 TTFB 更慢，见《分句流式TTS-TTFB优化方案》fp16 附录），已两机统一关闭；② vLLM 单 T4 可行性评估结论「不推荐」（实测 ASR+TTS 稳态占 5.87GB/15GB，vLLM 预分配 KV 池与之强冲突 + Turing 不支持 bf16 + torch 需升级，见《T4显存限制下vLLM可行性评估报告》））|

---

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| GPU 资源/成本 | MVP 单卡共享 ASR+TTS；实测 T4 16GB 即可，成本低于原估的 A10 |
| 浏览器音频格式杂乱 | 前端降采样 16k + 后端 ffmpeg 兜底 |
| ~~公网备案周期长~~ | **已规避**：自签证书 + 公网 IP 直连，无需备案即上线；要域名时再并行推进备案 |
| 自签证书浏览器警告 | MVP 接受一次性「高级 → 继续前往」；需消除时换域名 + 受信任证书 |
| vLLM 与模型版本兼容 | 锁定 funasr>=1.3 / vllm>=0.12 验证过的组合（Phase 3） |
| 中文 TTS 长文本稳定性 | CosyVoice RAS 采样 + 文本归一化 + 分句合成 |

---

## 11. 待确认 / 需用户提供的资源清单

1. ✅ 阿里云账号 + GPU ECS 实例（M4，已开通：T4 16GB / <PUBLIC_HOST>）。
2. 公网域名 + ICP 备案 —— **MVP 未使用**（自签 IP 直连已绕开）；仅在要消除浏览器警告 / 域名访问时才需要。
3. ✅ HTTPS 证书（M4）—— 已用**自签证书**满足；后续可换 Let's Encrypt / 阿里云免费证书（需先有备案域名）。
4. （可选）OSS 存储桶用于音频持久化 —— 仍未启用。
5. ✅ ModelScope 下载 —— 已用，免登录即可拉取本 MVP 所需权重。

> 以上资源在对应里程碑到来时，我会明确告知并请你操作；涉及花钱的我只提示、不代购。
