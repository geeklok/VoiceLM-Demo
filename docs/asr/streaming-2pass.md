> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 实时录音转写：真流式 2pass + 字符修正方案（node1 灰度）

## 摘要（Summary）

把 ASR「实时录音转写」从当前的**伪流式**（攒满整段音频才跑一次 SenseVoice offline，过程中只回 `... (X.Xs)` 占位串）升级为**真流式 2pass**：

- **第一遍（低延迟出字）**：引入 `paraformer-zh-streaming` 真流式模型，按 600ms 聚合块持续增量出字，边录边显示临时结果（灰字）。
- **第二遍（字符修正）**：用流式 fsmn-vad 检测句子端点，句末把该句原始音频交给**已加载的 offline SenseVoice** 整句重解码，产出修正后的定稿（黑字），覆盖该句的临时字。
- **前端双区渲染**：已定稿段落黑字 + 当前句临时结果灰字；修正发生时可见「灰字 → 黑字」的回改。
- **灰度纪律**：全部能力**仅在 node1 经 env 开启**（`FUNASR_STREAMING_MODEL` + `DEFAULT_ASR_MODEL`）；node2 不配置这两个 env → 引擎不注册、行为保持现状基线，用于对比。完全可逆，无需改 node2。

设计目标：尽量降低字符输出延迟（首字 ≈ 一个 600ms 聚合窗 + 推理），同时保留整句修正以保证准确率。

---

## 现状分析（Current State Analysis）

### 后端
- [funasr_engine.py](../../backend/app/engines/funasr_engine.py#L123-L135) `transcribe_stream`：伪流式。攒 buffer，过程中 `yield ASRPartial(text="... (X.Xs)")`，结束才整段 `transcribe()` 出 final。
- [registry.py](../../backend/app/engines/registry.py#L28-L51) `_build_asr`：funasr 分支注册 `funasr-sensevoice`，可选 `funasr-paraformer-zh`（配 `funasr_paraformer_model` 才注册）。默认引擎 = `default_asr_model` 或第一个注册的。
- [dispatcher.py](../../backend/app/orchestration/dispatcher.py#L136-L151) `asr_stream`：`adapted()` 逐块 `preprocess_pcm(...→16k)`，在 `asr_slot()` 信号量内**整条流持有许可**，透传引擎的 partial/final。不走降级/熔断/超时。
- [routes_asr.py](../../backend/app/api/routes_asr.py#L55-L116) `/ws/asr`：首帧 start JSON（sample_rate/channels/language/model）；后续二进制 float32 PCM；`{type:"end"}` 结束。下行 `{type:"partial"/"final", text, segment_id, node}`。**协议已带 `segment_id`，无需改协议字段**。
- [base.py](../../backend/app/engines/base.py#L16-L20) `ASRPartial(text, is_final=False, segment_id=0)` —— 字段已够用，无需改。`expected_sample_rate=16000`。
- [config.py](../../backend/app/config.py)：**无任何流式专属配置**，需新增。
- [routes_health.py](../../backend/app/api/routes_health.py#L39-L52) `/api/v1/models`：按 `reg.asr_engines` 列出引擎，新引擎注册后会自动出现在前端模型下拉。

### 前端
- [AsrPage.tsx](../../frontend/src/pages/AsrPage.tsx#L81-L92)：`openAsrStream` 回调里无论 partial/final 都 `setText(t)` **整体覆盖**，`isFinal` 仅改 meta 文案。无 partial/final 视觉区分。停录只发 `{type:"end"}` 未 `ws.close()`（潜在泄漏）。
- [client.ts](../../frontend/src/api/client.ts#L110-L131)：`openAsrStream` 的 `onPartial(text, isFinal, node)` **未透传 `segment_id`**，需要补。
- [recorder.ts](../../frontend/src/audio/recorder.ts)：`ScriptProcessorNode(4096)`，浏览器端 `downsample` 到 16kHz，发 float32 raw PCM，每块 ~85ms。**本方案不改 recorder**（后端做聚合）。
- [styles.css](../../frontend/src/styles.css#L83-L92)：`.result` 黑底卡片。新增 partial 灰字样式。

### 关键语义约定（贯穿前后端）
`ASRPartial.text` 改为承载**「当前这一句」的文本**（不是累计全文）：
- `is_final=False`：第 N 句的流式临时结果（灰字）。
- `is_final=True`：第 N 句经 offline 修正的定稿（黑字，覆盖该句灰字）。
- `segment_id`：句序号。

前端按 `segment_id` 累积：`已定稿句数组.join("")`（黑） + `当前临时句`（灰）。

---

## 改动清单（Proposed Changes）

### 1. 后端配置 —— [config.py](../../backend/app/config.py)
在 FunASR 段（约 line 48 之后、`funasr_paraformer_model` 附近）新增：

```python
# ---- 真流式 ASR (Phase 3: 实时录音转写 2pass) ----
# paraformer 流式模型 id/路径; 留空 = 不注册流式引擎 (节点保持伪流式基线)。
funasr_streaming_model: str = ""
# 聚合窗 (ms): 把前端 ~85ms 小块聚合到该时长再喂模型, 平衡延迟与精度。
funasr_streaming_chunk_ms: int = 600
# paraformer 流式 chunk_size [回看, 当前, 前看], 600ms 对应 [0,10,5]。
funasr_streaming_chunk_size: list[int] = [0, 10, 5]
funasr_streaming_encoder_look_back: int = 4
funasr_streaming_decoder_look_back: int = 1
# 2pass 修正: 句末用 offline SenseVoice 整句重解码覆盖流式临时字。
funasr_stream_correct_enabled: bool = True
```

> `default_asr_model` 已存在（line 56），灰度时由 node1 env 设为流式引擎名。

### 2. 后端流式引擎 —— [funasr_engine.py](../../backend/app/engines/funasr_engine.py)
**新增类 `FunASRStreamingEngine(ASREngine)`**（与现有 `FunASREngine` 并列，不改后者逻辑）：

- **构造**：`__init__(settings, *, name="funasr-streaming", offline_engine: FunASREngine)`。持有 `offline_engine` 引用（即已注册的 `funasr-sensevoice`，2pass 修正复用它，零额外显存）。
- **`_load()`**：懒加载两个流式模型实例（均 `disable_update=True, device=settings.device`）：
  - `self._asr = AutoModel(model=settings.funasr_streaming_model)` —— paraformer 流式（**不挂 vad**，流式 generate 自带切块）。
  - `self._vad = AutoModel(model=settings.funasr_vad_model)` —— fsmn-vad，流式端点检测用（权重已下载，复用）。
- **`transcribe()`（文件式，ABC 必须实现）**：直接 `return await self.offline_engine.transcribe(pcm, language, hotwords)` —— 文件上传走 offline SenseVoice，与基线同质量。
- **`transcribe_stream()`（核心 2pass）**，伪代码：

```python
async def transcribe_stream(self, chunks, language="auto"):
    self._load()
    win = int(self.expected_sample_rate * settings.funasr_streaming_chunk_ms / 1000)
    buf = []                       # 待聚合的小块
    seg_audio = []                 # 当前句累计原始音频 (供 offline 修正)
    seg_text = ""                  # 当前句流式累计文本
    seg_id = 0
    para_cache, vad_cache = {}, {}

    async def flush(pcm_block, is_final):
        nonlocal seg_text
        # 1pass: paraformer 流式增量出字
        piece = await to_thread(self._asr.generate, input=pcm_block, cache=para_cache,
                                is_final=is_final, chunk_size=..., encoder_chunk_look_back=...,
                                decoder_chunk_look_back=...)
        seg_text += piece_text(piece)
        # vad 流式端点检测
        ended = await to_thread(self._vad.generate, input=pcm_block, cache=vad_cache,
                                is_final=is_final, chunk_size=settings.funasr_streaming_chunk_ms)
        return seg_text, vad_endpoint_reached(ended)

    async for chunk in chunks:
        buf.append(chunk); seg_audio.append(chunk)
        if total(buf) >= win:
            block = concat(buf); buf = []
            text, ended = await flush(block, is_final=False)
            yield ASRPartial(text=text, is_final=False, segment_id=seg_id)   # 灰字
            if ended and funasr_stream_correct_enabled:
                corrected = (await self.offline_engine.transcribe(concat(seg_audio), language)).text
                yield ASRPartial(text=corrected or text, is_final=True, segment_id=seg_id)  # 黑字
                seg_id += 1; seg_text = ""; seg_audio = []; para_cache, vad_cache = {}, {}
            elif ended:
                yield ASRPartial(text=text, is_final=True, segment_id=seg_id)
                seg_id += 1; seg_text = ""; seg_audio = []; para_cache, vad_cache = {}, {}

    # 收尾: flush 残余 + 末句修正
    if buf or seg_audio:
        text, _ = await flush(concat(buf) if buf else zeros, is_final=True)
        if seg_audio and funasr_stream_correct_enabled:
            text = (await self.offline_engine.transcribe(concat(seg_audio), language)).text or text
        if text:
            yield ASRPartial(text=text, is_final=True, segment_id=seg_id)
```

要点：
- 所有 funasr 同步调用用 `asyncio.to_thread` 包裹（与现有 `transcribe` 一致）。
- `vad_endpoint_reached`：fsmn-vad 流式返回 `res[0]["value"]` 为 `[beg,end]` 列表，出现 `end != -1` 即句子结束。
- offline 修正失败/空结果时回退到流式 `text`（不阻断）。
- `warmup()`：`_load()` + 用 1s 静音跑一遍流式 generate 触发权重加载/编译。

### 3. 后端注册 —— [registry.py](../../backend/app/engines/registry.py#L28-L51)
在 `_build_asr` funasr 分支末尾追加：

```python
if s.funasr_streaming_model:
    from app.engines.funasr_engine import FunASRStreamingEngine
    streaming = FunASRStreamingEngine(s, name="funasr-streaming", offline_engine=sense)
    self._asr[streaming.name] = streaming
```

默认引擎仍由 `_resolve_default_asr()`（读 `default_asr_model`）决定 —— 灰度时 node1 env 设 `DEFAULT_ASR_MODEL=funasr-streaming`，则文件 + 实时都走该引擎（文件经 `transcribe()` 委托 offline，实时走 2pass）。

### 4. 前端 API —— [client.ts](../../frontend/src/api/client.ts#L110-L131)
`openAsrStream` 的 `onPartial` 透传 `segment_id`：

```ts
onPartial: (text: string, isFinal: boolean, segmentId: number, node?: string) => void
...
if (msg.type === "partial") onPartial(msg.text, false, msg.segment_id ?? 0, msg.node);
else if (msg.type === "final") onPartial(msg.text, true, msg.segment_id ?? 0, msg.node);
```

### 5. 前端双区渲染 —— [AsrPage.tsx](../../frontend/src/pages/AsrPage.tsx)
- 状态从单 `text` 改为按句模型：`committed: Record<number, string>`（已定稿）+ `partial: { id, text } | null`（当前临时句）。
- `openAsrStream` 回调：
  - `isFinal=true`：`committed[segmentId] = text`，并清掉同 id 的 partial。
  - `isFinal=false`：`partial = { id: segmentId, text }`。
- 渲染：已定稿句拼接为黑字 `<span>`，当前 partial 为灰字 `<span className="asr-partial">`。
- `toggleRecord` 停录补 `wsRef.current?.close()`（修当前泄漏）；开录前重置 committed/partial。

### 6. 前端样式 —— [styles.css](../../frontend/src/styles.css#L83-L92)
新增：
```css
.asr-partial { color: var(--muted); text-decoration: underline; text-underline-offset: 3px; }
```

---

## 灰度部署（node1，须先告知用户的环境变更）

> 以下涉及**下载新模型权重（环境变更）**，执行前向用户确认。

1. **下载流式模型**（~1GB，阿里 ModelScope 源）：在 node1 `/opt/asr` 用 warmup 自动拉取，或扩展 `scripts/download_models.py` 的 `ASR_MODELS` 增加 `paraformer-zh-streaming` 后跑一次。
2. **node1 `/opt/asr/deploy/.env.deploy`**（先 `cp` 备份）新增：
   ```
   FUNASR_STREAMING_MODEL=paraformer-zh-streaming
   DEFAULT_ASR_MODEL=funasr-streaming
   ```
3. 代码改动须重建镜像：`docker compose build backend && docker compose up -d backend`（前端同理 build）。
4. **node2 不动**：不配置上述两个 env → `FunASRStreamingEngine` 不注册，保持伪流式基线，用于对比。

---

## 假设与决策（Assumptions & Decisions）

- **方案**：2pass（paraformer 流式 + offline SenseVoice 修正）—— 用户已选。
- **前端**：双区区分渲染（黑/灰）—— 用户已选。
- **范围**：先 node1 灰度，node2 基线对照 —— 用户已选。
- `ASRPartial.text` 语义由「累计全文」改为「当前句文本」，前端按 `segment_id` 累积。协议字段无需新增（`segment_id` 已存在）。
- 流式引擎复用已加载的 offline SenseVoice 做 2pass，**零额外大模型显存**；仅新增 paraformer 流式（~1GB）+ 流式 vad 实例（权重复用）。T4 当前空闲 ~10GB，充足。
- recorder 不改（后端做 600ms 聚合）；dispatcher 不改（透传引擎 partial，引擎自身在 `asr_slot` 内完成 1pass+2pass）。
- 流式会话整条持有 `asr_slot`，`asr_concurrency=2` → 最多 2 路并发实时转写（灰度够用）。

## 验证（Verification）

1. **后端单测**：现有 [test_api.py::test_ws_asr_stream](../../backend/tests/test_api.py#L81-L93) 用 stub 引擎仍须通过（stub 默认，未开流式）。新增针对 `FunASRStreamingEngine` 的轻量单测（mock `_asr`/`_vad`/`offline_engine`）：喂多块 → 断言收到若干 `is_final=False` partial 后跟 `is_final=True` final、`segment_id` 递增、final 文本来自 offline。
2. **本地** `pytest` 全绿（stub 路径不受影响）。
3. **node1 灰度后端到端**：浏览器实时录音，确认（a）首字延迟约一个聚合窗 + 推理；（b）说话过程中灰字增量出现；（c）句末灰字被黑字替换（可见修正）；（d）停录 final 收尾、ws 关闭。
4. **node2 对照**：模型下拉无 `funasr-streaming`，录音仍为旧伪流式占位串行为，确认基线未受影响。
5. **显存核对**：node1 加载后 `nvidia-smi` 显存仍有余量（预期 +~1.5GB）。
6. **回退**：移除 node1 两个 env + `up -d backend` 即恢复基线（无数据迁移）。

---

## 部署后修复：LB 把实时 WS 轮询到了未部署该特性的 node2（2026-06-28）

### 现象
node1 实测正常，但实际使用中**约一半的实时录音会话不出字**。

### 根因
web/nginx LB 只在 node1，但 [nginx.conf](../../frontend/nginx.conf) 的 `/ws/` 用 `least_conn` 在**两台 backend 间轮询**（node1 + node2 `<NODE2_PRIVATE_HOST>:8000`）。灰度只把流式引擎部署到 node1，node2 仍跑伪流式基线 [transcribe_stream](../../backend/app/engines/funasr_engine.py#L123-L135)：录音过程只回 `... (X.Xs)` 占位串，真文字只在停录后作为 final 出现。而前端停录即 `ws.close()`，node2 的 final（需整段离线重解码数秒）来不及到达就被关闭丢弃 → 屏幕无真文字。直连 node2 实测确认：`partials=66` 全是占位串，`finals=1` 在 close 边界。

附带隐患：`/api/v1/models` 同样被 LB 轮询，落到 node2 会让前端默认引擎退回 `funasr-sensevoice`。

### 修复（仅改 nginx 路由，纯配置可逆）
[nginx.conf](../../frontend/nginx.conf) 新增灰度专用上游 `backend_node1`（只含本机 backend），并把两个 location 钉到它：
- `location /ws/asr` → `backend_node1`（实时流式只走 node1）。
- `location = /api/v1/models` → `backend_node1`（模型发现稳定返回 node1 的 `funasr-streaming` 默认）。
- 文件 ASR `/api/`、流式 TTS `/ws/tts` 仍走双机 LB（HA + node2 基线对照不变）。

web 本就只在 node1，钉定不引入额外单点。

### 验证（经 nginx LB 实测）
- `/ws/asr` ×6：全部 `node=node1`、`real_partials=6 placeholder=0`、有修正 final（修复前为 ~50% 轮询到 node2）。
- `/ws/tts` ×6、`/api/v1/asr` ×6：仍 node1/node2 交替，node2 文件 ASR 保持 `funasr-sensevoice` 基线。
- `nginx -t` 通过；node2 容器未动（healthy）。

### 回退
还原 [nginx.conf](../../frontend/nginx.conf) 的 `/ws/` 与 `/api/` 为单一 `backend` 上游、删除 `backend_node1`，`deploy/sync.sh --web-only` 即可。

---

## 全量铺开：真流式同步到 node2，撤销钉定回归双机 LB（2026-06-28）

### 背景
钉定只是临时止血——让一半算力（node2）闲置在文件 ASR 上、实时录音只能单机 node1 扛。用户决策：**把真流式同步到 node2，回到双机负载均衡**（HA + 算力翻倍）。

### 关键约束/发现
- backend 业务代码**烤进镜像**（Dockerfile `COPY app`），非挂载卷。node2 当时跑旧基线镜像（image id `9b268cb8`，无流式代码），故仅改 env 不够，**必须把 node1 的新镜像（`b8d203c1`，含 `FunASRStreamingEngine`）整体送到 node2**。
- node2 backend 端口绑内网 `<NODE2_PRIVATE_HOST>:8000`（override），公网 `<NODE_PUBLIC_HOST>` 仅供运维 SSH。
- node1→node2 的 SSH 互信在初次接入后已按安全约定删除，本次**临时重建**（ephemeral ed25519，完事即从两机清除）。

### 执行（全程走 VPC 内网，不经本机中转 GB 级数据）
1. 临时互信：node1 生成 `n2_tmp` 密钥，公钥加到 node2 `authorized_keys`。
2. 镜像直传：node1 `docker save asr-tts-backend:latest | ssh node2 docker load`（17.3GB，内网约 2 分 23 秒）。
3. 模型直传：`rsync` node1 的 849M `...online` 流式模型到 node2 `deploy/models/`（约 3 秒）。
4. node2 配置：`.env.deploy` 备份后设 `DEFAULT_ASR_MODEL=funasr-streaming` + 追加 `FUNASR_STREAMING_MODEL=/models/...online`（与 node1 一致）。
5. node2 重建：`docker compose up -d backend`（重新加载全套模型，~1-3 min 预热）。
6. 撤钉定：[nginx.conf](../../frontend/nginx.conf) 删除 `backend_node1` 上游与 `/ws/asr`、`= /api/v1/models` 两个钉定 location，`/ws/asr` 回落 `/ws/`、`/api/v1/models` 回落 `/api/`（均双机 LB）；`deploy/sync.sh --no-build --web-only` 部署。
7. 清理：移除两机临时互信密钥、传输日志、容器内测试脚本。

### 验证
- node2 单机（容器内 `ws://127.0.0.1:8000/ws/asr`）：`node=node2 real=8 placeholder=0 finals=1`，增量 partial（欢迎大→…）+ offline 修正 final，RESULT PASS；GPU 4933→5799 MiB（+866 流式模型，与 node1 一致）。
- **经公网 nginx LB `/ws/asr` ×6**：node1/node2 各 3 次交替，**两机均** `real=8 placeholder=0 finals=1`、final 同为「欢迎大家来体验达摩院推出的语音识别模型。」，`ALL_PASS=True`（修复前 node2 那一半只出占位串）。
- `nginx -t` 通过；部署配置已无 `backend_node1`；两机 backend healthy，GPU 均 5799/15360 MiB。

### 现状
两台节点默认引擎均为 `funasr-streaming`，实时录音 WS 双机负载均衡，故障可互相兜底。`backend_node1` 钉定上游已彻底移除。

### 回退（若需退回单机）
node2：`.env.deploy` 还原备份（`DEFAULT_ASR_MODEL=funasr-sensevoice`、删 `FUNASR_STREAMING_MODEL`）+ `docker compose up -d backend`；如需同时把 `/ws/asr` 收回 node1，按上一节重新加 `backend_node1` 钉定即可。

---

## 热词增强：streaming 动态定稿（seaco / SenseVoice 句末切换）+ 能力标志下发（2026-07-01）

### 背景与目标
真流式 2pass 的第一遍是 `paraformer-online`，**架构上不支持热词偏置**——滚动临时字永远无热词。而线上有「大神/大大/三茂」这类专有名词强需求。本次在**不牺牲 SenseVoice 多语种 + 情感富文本（😡😊 emoji）**的前提下，给流式加上真热词：**句末定稿引擎动态选择**——带热词的会话用 SeacoParaformer（唯一支持 bias encoder 的引擎）重解码，不带热词仍走 SenseVoice。两引擎均已常驻，**零额外大模型显存**。

同时解决一个体验问题：不是所有 ASR 模型都支持热词（SenseVoice、paraformer-online 都不支持），前端却一直显示可编辑的热词框，误导用户。本次引入**能力标志 `supports_hotwords`**，经 `/api/v1/models` 下发，前端据此禁用不支持模型的热词框。

### 后端改动

**1. 能力标志 `supports_hotwords`（基类 + 下发）**
- [base.py](../../backend/app/engines/base.py#L34-L36) `ASREngine` 加类属性 `supports_hotwords: bool = False`；`transcribe_stream` 签名加 `hotwords: Optional[list[str]] = None`。
- [routes_health.py](../../backend/app/api/routes_health.py#L44-L51) `/api/v1/models` 每个 ASR 条目回填 `supports_hotwords=e.supports_hotwords`。
- [models.py](../../backend/app/schemas/models.py) `ModelInfo` 加 `supports_hotwords: bool = False`。
- 各引擎取值：`funasr-sensevoice`=False；`funasr-seaco`=True；`funasr-streaming`=**条件性**（注入了 seaco 才 True）。**不在前端硬编码模型名**，因 streaming 是否支持热词取决于部署有没有配 seaco。

**2. SeacoParaformer 热词引擎接入**
- [registry.py](../../backend/app/engines/registry.py#L47-L74)：`funasr_seaco_model` 配了才注册 `funasr-seaco`（`flavor="paraformer"` 复用同一 transcribe 路径 + 挂 `punc_model` 恢复标点），构造时 `supports_hotwords=True`。
- 官方模型 id：`iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`（**下划线**，容器内目录名对应 `iic__speech_seaco_...`）。

**3. streaming 动态定稿（核心）**
- [funasr_engine.py](../../backend/app/engines/funasr_engine.py#L161-L177) `FunASRStreamingEngine.__init__` 加 `hotword_engine`（注入 seaco），`self.supports_hotwords = hotword_engine is not None`。
- 新增 [_finalize_engine](../../backend/app/engines/funasr_engine.py#L220-L224)：定稿引擎选择——带热词且已注入 hotword_engine → 用 seaco，否则用 offline（SenseVoice）。未配 seaco 时热词被静默忽略。

  ```python
  def _finalize_engine(self, hotwords: Optional[list[str]]) -> ASREngine:
      """定稿引擎: 带热词且已注入 hotword_engine 时用它, 否则用 offline。"""
      if hotwords and self._hotword is not None:
          return self._hotword
      return self._offline
  ```

- [transcribe_stream](../../backend/app/engines/funasr_engine.py#L249-L322) 开头 `finalize = self._finalize_engine(hotwords)`（**整轮固定，不逐句切**），句末 `_correct` 用 `finalize.transcribe(pcm, language=language, hotwords=hotwords)`。文件式 [transcribe](../../backend/app/engines/funasr_engine.py#L226-L231) 同样委托 `_finalize_engine`。
- **第一遍滚动临时字始终无热词**（paraformer-online 限制），句末被定稿整句覆盖——用户只在灰字→黑字回改时看到热词生效。

**4. 协议透传**
- [routes_asr.py](../../backend/app/api/routes_asr.py#L76-L80) `/ws/asr` start 帧解析 `hotwords`（逗号分隔字符串，与文件式一致）：`[w.strip() for w in hw.split(",") if w.strip()] or None`。
- [dispatcher.py](../../backend/app/orchestration/dispatcher.py) `asr_stream` 透传 `hotwords`。

### 前端改动
- [client.ts](../../frontend/src/api/client.ts#L111-L131)：`ModelInfo` 加 `supports_hotwords?: boolean`；`openAsrStream` 加 `hotwords?: string` 形参，start 帧带 `hotwords: hotwords || undefined`。
- [AsrPage.tsx](../../frontend/src/pages/AsrPage.tsx#L39-L41)：派生 `hotwordsSupported = selectedModel ? !!selectedModel.supports_hotwords : true`（列表未加载时默认允许，交后端忽略）；不支持时**禁用热词输入框**并给灰字提示「该模型不支持热词, 请选用 funasr-seaco」，文件式/流式两条路径都按 `hotwordsSupported` 决定是否带热词。

### 部署（node1 + node2 均已上线）
两机现均常驻 5 个引擎位（实际注册取决于 env）：`funasr-sensevoice`（默认富文本）、`funasr-seaco`（热词）、`funasr-streaming`（默认，动态定稿）。`funasr-paraformer-zh` 已下线（seaco 是其功能超集，不给热词时退化同一基座，实测逐字一致）。

- **node1**（Docker Hub 可达）：常规 `docker compose build backend && up -d backend` + 前端 `npm run build` 后 rebuild web。
- **node2**（backend-only，**Docker Hub 不可达**）：本次纯 Python 改动、无新依赖，用 **overlay 增量构建**规避拉不到 base 镜像的问题（详见《水平扩容Runbook-单机LB骨架》§3.6）：

  ```dockerfile
  # /opt/asr/backend/Dockerfile.node2overlay
  FROM asr-tts-backend:latest
  COPY app /app/app
  ```

  只换 Python 源码、零联网、不动 17GB 依赖、不传镜像。seaco 权重经 ModelScope 现下；`.env.deploy` 注释 `FUNASR_PARAFORMER_MODEL`、追加 `FUNASR_SEACO_MODEL`（与 node1 对齐，改前备份原文件）。

### 验证（同一段 vad_example.wav，node2 经容器内 `ws://127.0.0.1:8000/ws/asr`）
`partials=117 / finals=4` 两轮完全一致，**仅句末定稿切换**：

| 会话 | 定稿引擎 | 「搭神」 | 「哒哒」 | emoji |
|------|---------|--------|--------|-------|
| 不带热词 | SenseVoice | 搭神 ×2（错） | 哒哒（错） | 有 😡😊 |
| 带 `大神,大大,三茂` | seaco | **大神 ×2**（对） | **大大**（对） | 无（seaco 不产 emoji） |

- node1 与 node2 逐字一致；两机 `/api/v1/models` 标志一致：`sensevoice=False, seaco=True, streaming=True`。
- 显存：node1 5873/15360 MiB、node2 3201/15360 MiB（node2 更低——去掉了独立 paraformer-zh）。GPU 无额外占用（seaco 是取代 paraformer 而非新增）。
- 本地 64 tests 全绿（含新增 [test_registry.py](../../backend/tests/test_registry.py) 两条：seaco 支持热词且 streaming 注入后继承、无 seaco 时 streaming 不支持热词）；前端 `tsc -b && vite build` 通过。

### 关键决策 / 踩坑
- **动态定稿引擎整轮固定**（不逐句切）：`transcribe_stream` 开头选一次。避免同一段音频里定稿风格跳变，也简化 cache 管理。
- **能力标志而非硬编码模型名**：streaming 是否支持热词取决于部署有没有配 seaco，前端只能依赖后端下发的 `supports_hotwords`，不能写死「streaming 支持热词」。
- **seaco 顶替 paraformer-zh**：两者同基座，seaco 是超集（多热词偏置能力），保留两个属冗余显存浪费。下线后 node2 显存直降 ~2.6GB。
- **node2 无 emoji 是预期**：带热词走 seaco，seaco 不产情感 emoji；要 emoji 就别传热词（走 SenseVoice）。这是「热词 vs 富文本」的取舍，二选一。

---

## 聚合窗低延迟优化：600ms → 480ms（2026-07-05，node1 灰度实测正面）

> 背景：TTS 加速三条路线（fp16/vLLM/量化）经实测/评估均对延迟无正收益（见《T4显存限制下vLLM可行性评估报告》《分句流式TTS-TTFB优化方案》fp16 附录）后，转向 ASR 首字延迟——聚合窗是零成本、可回退、直接命中首字延迟的旋钮。

### 关键约束（改动前必须理解）
`funasr_streaming_chunk_ms`（聚合窗）与 `funasr_streaming_chunk_size[1]`（paraformer 解码「当前帧数」）**强耦合**：paraformer-streaming 每帧 60ms，故 `chunk_ms` 必须 = `chunk_size[1] × 60`，否则聚合窗与解码窗错位。
- 600ms = 10 帧 → `[0,10,5]`（基线）
- **480ms = 8 帧 → `[0,8,4]`（本次，前看 5→4 帧同步收窄）**
- 360ms = 6 帧 → `[0,6,3]`（更激进，未采纳）
- **严格 400ms 不可行**：400÷60 = 6.67 帧不整除，480ms 是「最接近 400 且整除」的正确值。
- 注：`chunk_ms` 还被 fsmn-vad 复用为 `chunk_size`（[funasr_engine.py](../../backend/app/engines/funasr_engine.py#L306)），一并生效。

### 落地方式（纯配置，不改代码；config 默认仍 600 作对照基线）
node1 `/opt/asr/deploy/.env.deploy` 追加（`list[int]` 走 JSON 格式，已实测 pydantic-settings 正确解析）：
```
FUNASR_STREAMING_CHUNK_MS=480
FUNASR_STREAMING_CHUNK_SIZE=[0,8,4]
```
`docker compose up -d backend` recreate 生效（env_file 变更需 recreate，非 restart）。node2 维持 600ms 作对照。

### 实测结果（同一 70.5s 音频 × 4 轮，~85ms 小块实时喂 `/ws/asr`）
| 指标 | node1 (480ms) | node2 (600ms) | 收益 |
| --- | --- | --- | --- |
| **首字延迟中位数**（首个 partial 出字） | **1025.9 ms** | 1375.5 ms | **↓349.6 ms（-25.4%）** |
| partial 数（出字密度） | 138 | 103 | +34%（更密更流畅） |
| finals（定稿句数） | 4 | 4 | **断句无退化** |
| 4 轮方差 | sd<7 | sd<9 | 数据稳定可信 |

### 结论与取舍
- **首字延迟 ↓25.4%（349ms）**，比预估的 ~120ms 更好（窗口缩短 + 前看帧 5→4 双重提前）；partial 密度 +34%，实时感增强；finals 句数一致，最担心的「窗口变小导致断句错乱」未发生。
- 与 fp16/量化的负面结果形成对比：**聚合窗是真正命中延迟瓶颈、零成本、可回退的有效优化。**
- **字准对照（推全前逐字核验，2026-07-05）**：同一 vad_example.wav，node1(480) vs node2(600) 逐段 final 文本对比——**480ms 不仅无退化，反而 4 处边界更干净/更准**（seg0 结尾少一误字「活」、seg1「劳同」→「活动」、seg2 结尾「就」→「觉得」、人名「三炮」→「三茂」），0 处更差，断句 seg0-3 完全一致。坐实首字延迟↓25% 且字准不降。
- **回退**：删两行 env + recreate 即回 600ms，零风险。
- 状态（2026-07-05）：**已双机推全**——字准对照通过后从 node1 推全到 node2，两机均 `chunk_ms=480 / chunk_size=[0,8,4]`（各自 `.env.deploy` 改动前备份为 `.env.deploy.bak.chunkwin`）。压测/对比脚本为临时取证，已从两机 host/容器清理。config.py 默认仍 600（新部署未显式配 env 时的保守基线）。
