> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 分句流式 TTS — TTFB 优化方案 (B 线灰度)

## 摘要 (Summary)

CosyVoice2 的首包延迟 (TTFB) 随输入文本长度增长（len=3→~1.6s，len=89→~5s），根因在其 Qwen2 LLM 自回归段：必须先把**整段文本**喂进去、生成第一个语音 token 才返回首块。已验证 JIT、vLLM 都无法在当前环境干净地改善这一点（JIT 不在首包路径、vLLM 需升级 torch 大版本，均已留档）。

本方案改走**纯后端逻辑层**优化：把长文本按标点**分句**，在**同一个 TTS 信号量持有期内**逐句调用 `synthesize_stream`，让**第一短句**快速出首块，从而把 TTFB 绑定到一个短输入上（首句越短，LLM 段越快出第一个 token）。零新增依赖、零模型改动、引擎无关，完全适配现有 node1/node2 灰度对比基础设施。

- 默认关闭（`TTS_SENTENCE_STREAM=false`），行为与现状逐字节一致 → 不影响 node2 基线、不影响现有测试。
- 仅在 node1 打开做 after 对比；node2 保持基线作 before/after 锚点。

## 当前流式生命周期与音色语义（2026-07-18）

分句 TTFB 优化之外，流式 TTS 已补齐取消、背压和前端反馈：

- CosyVoice 同步生成器与异步 WebSocket 之间改为有界队列，水位由 `TTS_STREAM_QUEUE_CHUNKS` 控制，默认 8。队列满时生产线程等待消费者，避免长文本音频块无界占用内存。
- 客户端停止或断开时，路由显式 `aclose()` 流；引擎设置协作停止事件、关闭底层生成器并等待同步生产线程退出。外层 `GpuLimiter` 只有在线程结束后才释放 slot，避免旧请求仍占 GPU 时新请求进入。
- 被取消的流式请求记录为 `tts_requests_total{mode="stream",status="cancelled"}`；正常流仍在 `done.qos` 返回 TTFB、总处理时长、音频时长和 RTF。
- 前端只展示 `/api/v1/models` 返回的真实注册音色，未知音色返回 400 / WebSocket `bad_request`，不再伪造“中文男”等入口或静默回退默认音色。
- TTS 页支持停止流式合成、异常关闭后恢复按钮、字符计数、音频 object URL 主动回收，以及“金融金额 + 号码逐位读”的 TN 冲突提示。

当前生命周期：

```text
synthesize -> meta -> binary PCM... -> done(qos) -> close
      |                                  ^
      +-- client close -> stop event -> producer exit -> release GPU slot
```

## 现状分析 (Current State Analysis)

- **核心改造点** [dispatcher.py](../../backend/app/orchestration/dispatcher.py#L176-L228)：`tts_stream` 的 `guarded()` 内核当前在单个 `tts_slot()` 内对**整段 text** 调一次 `engine.synthesize_stream(text, ...)`。`ttfb_ms` 在首个 chunk 计时；`n_samples` 累加；流耗尽后填 `qos_holder` 并 `observe_tts`。
- **Dispatcher 不持有 Settings**，只接 scalars（[L35-L50](../../backend/app/orchestration/dispatcher.py#L35-L50)）。新 flag 须经 main.py thread 进构造函数（仿 `asr_infer_timeout`）。
- **信号量语义** [limiter.py]：`tts_slot()` 持有整个流；分句循环必须放在**同一个** `tts_slot()` 内，避免逐句重新排队触发 429。
- **配置** [config.py](../../backend/app/config.py#L79-L91)：flag 为带默认值的类属性，env 变量名 = 属性名大写。
- **WS 契约** [routes_tts.py](../../backend/app/api/routes_tts.py#L71-L86)：meta 首帧 → 若干 binary chunk → `done` 帧（含 qos_holder）→ close。**本方案不改此文件**，须保持契约。
- **引擎接口** [base.py](../../backend/app/engines/base.py#L71-L74)：`synthesize_stream(text, voice, speed)` 逐块 yield PCM。CosyVoice [cosyvoice_engine.py](../../backend/app/engines/cosyvoice_engine.py#L119-L146) 与 stub [stub_engine.py](../../backend/app/engines/stub_engine.py#L59-L65) 均满足。
- **无现成文本分句工具**：`backend/app/utils/` 下无 `text_split.py`（Glob 确认）。需新建。
- **测试**：[test_api.py:test_ws_tts_stream](../../backend/tests/test_api.py#L63-L78) 用 stub（text="你好"）验证 meta/≥1 binary/done；[test_dispatcher.py](../../backend/tests/test_dispatcher.py) 只覆盖 ASR，无 tts_stream 测试。
- **部署**：[docker-compose.yml](../../deploy/docker-compose.yml) backend `env_file: .env.deploy`；node1 现网 `/opt/asr/deploy/.env.deploy`（已回滚到干净基线：`COSYVOICE_LOAD_VLLM=false`/`COSYVOICE_FP16=true`/`NODE_NAME=node1`）。压测脚本 [tts_stream_loadtest.py](../../scripts/tts_stream_loadtest.py) 按 node 拆分输出。

## 拟议改动 (Proposed Changes)

### 1. 新建 `backend/app/utils/text_split.py`

**做什么**：提供 `split_sentences(text, max_chars) -> list[str]`，按中英文主标点切句，超长句再按次标点/硬截断切到 `max_chars` 以内；strip 空白、丢空串；空输入返回 `[]`。

**为什么**：让首句尽量短，绑定 TTFB 到短输入。引擎无关的纯函数，易测。

**怎么做**（草案，实施时以此为准）：

```python
from __future__ import annotations

# 主断句标点 (附在前句末尾): 中英文句末 + 分号 + 换行
_PRIMARY = "。！？!?；;\n"
# 次断句标点: 仅用于把超长句进一步切短
_SECONDARY = "，,、 "


def split_sentences(text: str, max_chars: int = 60) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    sents: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _PRIMARY:
            s = buf.strip()
            if s:
                sents.append(s)
            buf = ""
    if buf.strip():
        sents.append(buf.strip())

    out: list[str] = []
    for s in sents:
        out.extend(_cap(s, max_chars))
    return out


def _cap(s: str, max_chars: int) -> list[str]:
    if len(s) <= max_chars:
        return [s]
    pieces: list[str] = []
    buf = ""
    for ch in s:
        buf += ch
        if ch in _SECONDARY and len(buf) >= max_chars:
            pieces.append(buf.strip())
            buf = ""
    if buf.strip():
        pieces.append(buf.strip())
    final: list[str] = []
    for p in pieces:
        while len(p) > max_chars:
            final.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            final.append(p)
    return final or [s]
```

### 2. `backend/app/config.py` — 新增两个 flag

**做什么**：在 CosyVoice 配置块后新增：

```python
    # ---- 分句流式 TTS (Phase 3 §7 B 线 TTFB 优化) ----
    # 开启后: 长文本按标点分句, 在同一 TTS 信号量内逐句流式合成,
    # 让第一短句快速出首块以降 TTFB。默认关 = 行为与基线一致。
    tts_sentence_stream: bool = False
    # 单句最大字符数: 超出则按次标点/硬截断进一步切短。
    tts_max_sentence_chars: int = 60
```

**为什么**：env 可控开关，默认关保证 node2 基线与现有测试不受影响。

### 3. `backend/app/main.py` — 注入新 flag

**做什么**：在 [Dispatcher 构造](../../backend/app/main.py#L38-L45) 追加两参：

```python
    app.state.dispatcher = Dispatcher(
        registry,
        limiter,
        breaker,
        asr_fallback_enabled=settings.asr_fallback_enabled,
        asr_infer_timeout=settings.asr_infer_timeout,
        node_name=settings.node_name,
        tts_sentence_stream=settings.tts_sentence_stream,
        tts_max_sentence_chars=settings.tts_max_sentence_chars,
    )
```

**为什么**：Dispatcher 不持有 Settings，须显式 thread 进去（仿现有 scalar 风格）。

### 4. `backend/app/orchestration/dispatcher.py` — 分句循环改造

**做什么**：
- `__init__` 追加 keyword-only 参数 `tts_sentence_stream: bool = False`、`tts_max_sentence_chars: int = 60`，存为 `self._tts_sentence_stream` / `self._tts_max_sentence_chars`。
- 顶部 import：`from app.utils.text_split import split_sentences`。
- 改造 `tts_stream` 的 `guarded()`：在**单个** `tts_slot()` 内，先算出 `sentences`（关时 = `[text]`；开时 = `split_sentences(text, max_chars)`，结果为空则回退 `[text]`），再 **for 循环逐句** `engine.synthesize_stream(sentence, ...)`，`first`/`ttfb_ms` 只在**全局首个 chunk** 翻转，`n_samples` 跨句累加。`process_ms`/`audio_ms`/`rtf`/`observe_tts`/`qos_holder` 语义保持不变。

**改造后 `guarded()`（关键段）**：

```python
async def guarded() -> AsyncIterator[np.ndarray]:
    try:
        if self._tts_sentence_stream:
            sentences = split_sentences(text, self._tts_max_sentence_chars) or [text]
        else:
            sentences = [text]
        async with self._limiter.tts_slot():
            t0 = time.perf_counter()
            first = True
            ttfb_ms = None
            n_samples = 0
            for sentence in sentences:
                async for chunk in engine.synthesize_stream(
                    sentence, voice=voice, speed=speed
                ):
                    if first:
                        ttfb_ms = int((time.perf_counter() - t0) * 1000)
                        first = False
                    n_samples += len(chunk)
                    yield chunk
            process_ms = int((time.perf_counter() - t0) * 1000)
        audio_ms = int(n_samples / sr * 1000) if sr else 0
        rtf = (process_ms / audio_ms) if audio_ms else None
        observe_tts(
            engine.name, "stream", status="ok",
            process_ms=process_ms, ttfb_ms=ttfb_ms, rtf=rtf,
        )
        qos_holder.update(
            node=self._node, model=engine.name, ttfb_ms=ttfb_ms,
            process_ms=process_ms, audio_ms=audio_ms,
            rtf=round(rtf, 4) if rtf is not None else None,
        )
    except ConcurrencyLimitError:
        raise
    except Exception:
        observe_tts(engine.name, "stream", status="error")
        raise
```

**为什么**：单 `tts_slot()` 保证不被逐句重新排队（防 429）；全局首 chunk 计时让 TTFB 落到第一短句；其余 QoS 语义与 done 帧契约不变。

### 5. 测试

**`backend/tests/test_text_split.py`（新建）**：覆盖空串→`[]`、单句不切、多句按句号切、超长无标点句硬截断到 ≤max_chars、首句更短。

**`backend/tests/test_dispatcher.py`（追加）**：仿 `_ScriptedASR` 加一个最小 `_FakeTTS`（`synthesize_stream` 按 yield 短 PCM，记录收到的 sentence 列表）+ 最小带 `tts()` 的 registry，断言：开 flag 时多句文本被拆成多次 `synthesize_stream` 调用且 PCM 总量 = 各句之和；关 flag 时只调一次（整段）。`ttfb`/`qos_holder` 字段存在。

**回归**：[test_api.py:test_ws_tts_stream](../../backend/tests/test_api.py#L63-L78) 默认 flag 关 → 行为不变，须仍通过。

**本地运行**：`cd backend && .venv/bin/pytest -q`。

### 6. node1 灰度部署 + after 压测（实施阶段执行）

> ⚠️ 涉及现网变更与重建镜像，按既有约束在实施阶段进行；node2 不动（基线）。

1. 同步代码到 node1：`DEPLOY_HOST=<PUBLIC_HOST> DEPLOY_KEY=~/.ssh/your_key.pem deploy/sync.sh --no-build --no-restart`（仅同步后端代码，不动前端/容器）。或手工 rsync backend/ 到 `/opt/asr/`。
2. 改 node1 `/opt/asr/deploy/.env.deploy`：**备份后**追加 `TTS_SENTENCE_STREAM=true`（`TTS_MAX_SENTENCE_CHARS` 用默认 60，可不写）。node2 不加该行。
3. 重建并重启 node1 backend：`cd /opt/asr/deploy && docker compose build backend && docker compose up -d backend`。
4. 探活：容器内 `python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read())"` 期望 `ready:true`。
5. after 压测（打公网 LB，自动按 node 分流对比）：
   `python scripts/tts_stream_loadtest.py --url wss://your-domain.example.com/ws/tts --concurrency 2 --total 60 --warmup 4`
   重点看输出「按机器拆分」里 node1（优化版）vs node2（基线版）的 TTFB p50/p95。
6. 服务端真值以 Prometheus 为准：
   `histogram_quantile(0.5, sum(rate(tts_ttfb_seconds_bucket[5m])) by (le, node))`。
7. 把 before/after 对比表写入 [可观测性Runbook §6.4](../observability/qos-monitoring.md)。

## 假设与决策 (Assumptions & Decisions)

- **默认关闭**：`tts_sentence_stream=False` 时 `sentences=[text]`，逐字节等价于现状 → node2 与所有现有测试不受影响。
- **单 `tts_slot()` 包整个分句循环**：避免逐句重排队 429；代价是长文本占槽时间不变（总合成时长不变，只前移首块）。
- **TTFB 绑定首句**：多句文本（压测语料含标点）首句通常很短 → TTFB 显著下降是预期收益；无标点超长句只能硬截断到 60 字，首块改善有限（已知局限，留档）。
- **音质权衡**：逐句独立合成会丢跨句韵律、句间可能有轻微接缝（CosyVoice 每次调用边界）。MVP 可接受，不做 crossfade（避免过度设计）。如后续不满意再评估重叠拼接。
- **`process_ms`/`rtf` 含义不变**：仍是「整段总合成耗时 / 总音频时长」，分句不改变总量级，只改 TTFB。
- **不改 WS/file 路由、不改引擎、不改 limiter**：改动面收敛在 config/main/dispatcher + 新 util + 测试。
- **`tts_file`（非流式）不改**：分句优化只对流式 TTFB 有意义；文件式一次性返回，分句无收益。

## 验证 (Verification)

1. `cd backend && .venv/bin/pytest -q` 全绿（新增 text_split/dispatcher 测试 + 现有回归）。
2. 本地 stub 手测（可选）：起 `uvicorn`，flag 关/开各跑一次 WS，确认 done 帧与音频正常。
3. node1 重建后 `/readyz` = `ready:true`；node2 `/readyz` 不受影响。
4. after 压测：node1 TTFB p50/p95 显著低于 node2 基线（node2 应与 before 一致，证明测量可靠）；RTF 量级不恶化。
5. Prometheus `tts_ttfb_seconds` by node 佐证（网络无关真值）。
6. 结论（无论正负）如实写入 Runbook §6.4。

---

## 附：TTS 文本前处理 — markdown 剥离（2026-07-01，已落地双机）

### 背景（用户反馈 + 实锤定位）
用户反馈「TTS 输入 markdown（带 `**`、`#`、链接等特殊符号）时读错」。在 node1 容器内直接对 CosyVoice 的 `frontend.text_normalize` 灌各类 markdown 实锤，确认：

- **不是模型能力不足**，而是**服务端缺了「markdown → 纯文本」这层前处理**。
- CosyVoice2 的 `text_normalize` 只做 **TN（文本归一化：数字读法、标点规整、中英混排）**，**不剥离任何 markdown 语法**。于是 `#*_`` ``、`[文字](url)`、竖线 `|`、裸 URL 全部原样进 tokenizer，被逐字念出或打乱韵律。

实锤输出（节选，输入 → 归一化后送模型的文本）：

| 输入 | text_normalize 输出 | 问题 |
|---|---|---|
| `**重点**` | `这是**重点**内容。` | 星号残留被念 |
| `# 一级标题` | `#一级标题。` | 井号被念 |
| `[官方文档](https://example.com/docs)` | `详见[官方文档](https://example。com/docs)说明` | 括号 + **整条 URL 逐字念** |
| `https://example.com/a/b?x=1` | `https://example。com/a/b?x=一查看` | 裸 URL 逐字念 |
| `1. 第一步` | `一。第一步二。第二步。` | 序号混进句子 |
| `\| 姓名 \| 年龄 \|` | `\|姓名\|年龄\|...` | 竖线全念、读法崩坏 |

### 改动（引擎无关，file + stream 两条路径共用）
新建 [text_clean.py](../../backend/app/utils/text_clean.py) 提供纯函数 `strip_markdown(text) -> str`，在**送引擎合成前**把 markdown 转成适合朗读的纯文本：

- **链接 `[文字](url)` → `文字`**（留可读文字、丢 URL）；**图片 `![alt](url)` 整体删除**；**裸 URL / `www.` 删除**。
- 去行内强调/代码符号 `** __ * _ ~~ `` ` `；**代码围栏 ```` ``` ```` 去围栏、保留内部代码文本**（让代码正常朗读）。
- 行首标记转停顿：`#` 标题、`>` 引用、`-*+` 无序列表、`1. / 2)` 有序列表一律去掉行首标记。
- **表格**：分隔行 `|---|` 删除，单元格竖线 `|` → 中文逗号（转停顿，避免 `|姓名|年龄|` 连读），并修剪每行首尾多余逗号。
- 折叠多余空白/空行，逐行 strip 后丢空行。

接入点在 [dispatcher.py](../../backend/app/orchestration/dispatcher.py)：
- `tts_file`：`text = strip_markdown(text)`（在 `synthesize` 前）。
- `tts_stream` 的 `guarded()`：`clean = strip_markdown(text)` **在 `split_sentences` 之前**（即先剥 markdown 再分句，两个优化正交叠加）。

### 关键决策 / 边界
- **前处理层，不碰模型 / 不碰引擎接口**：与「分句流式」同属 dispatcher 入口的纯逻辑层，零新增依赖、零模型改动，引擎无关（stub / CosyVoice 都受益）。
- **代码围栏保留内部代码而非删除**：用户把代码块喂 TTS 通常是想听内容，删掉不如念出来；只去掉三反引号围栏。
- **裸 URL 直接删而非念**：URL 逐字朗读几乎无信息价值且极长，删除体验最好；带描述文字的 markdown 链接则保留描述文字。
- **表格竖线转逗号（停顿）而非删除**：保留单元格之间的语义边界，避免相邻单元格文字连读成一个词。
- **无条件生效（不设开关）**：纯文本经过 `strip_markdown` 基本不变（只折叠空白），对非 markdown 输入无副作用，故不像分句流式那样加 env flag。

### 验证 + 部署
- 新增 [test_text_clean.py](../../backend/tests/test_text_clean.py) 15 用例（bold/italic/heading/code/link/image/bare_url/list/table/quote/mix），本地 stub 全套 **79 passed**。
- **node1**（<NODE2_PUBLIC_HOST>）：常规 `docker compose build backend && up -d backend`，15s healthy。
- **node2**（<NODE_PUBLIC_HOST>，Docker Hub 不可达）：**overlay 增量构建**（`FROM asr-tts-backend:latest` + `COPY app`，零联网，见《水平扩容Runbook-单机LB骨架》§3.6），15s healthy。
- 两机均校验 `strip_markdown` 已入镜像（`## 标题\n**粗** 见 [文档](url)` → `标题\n粗 见 文档`）；真实 `/api/v1/tts` 带 markdown 请求返回 HTTP 200 `audio/wav`（node1 182KB / node2 204KB），无回归。

## 附：fp16 推理灰度对照 —— 负面结论，未采纳（数据快照 2026-07-05）

> 背景：三大能力延迟优化技术分析时，提出 `cosyvoice_fp16=True` 作为「一行开关、T4 原生支持、预估 TTFB↓30–40%」的高 ROI 候选。按 A/B 规范做双机灰度对照实测，**结论为负面：fp16 在 T4 + CosyVoice2 上无正收益，多数场景反而更慢**，故未采纳，两机统一保持 `COSYVOICE_FP16=false`（与 config.py 默认一致）。

### 实验设计
- **实验组** node1 fp16=True vs **对照组** node2 fp16=False（经 node-local `.env.deploy` 的 `COSYVOICE_FP16` 控制，config.py 默认 False 不改）。
- 固定语料 5 条（3/14/19/35/50 字，覆盖短中长句），每条 **20 轮交错**（外层轮次、内层遍历语料，减少时段偏差），走 `/ws/tts` 生产路径 + 常驻预热模型，读服务端 QoS 的 `ttfb_ms`/`rtf`。
- 两机同时段跑（node2 后台 + node1 前台并行，属两台不同机器各一条 SSH，非单机并发）。

### 结果（服务端 TTFB 中位数 ms / RTF 中位数）
| 语料(字数) | node1 fp16=True | node2 fp16=False | fp16 收益 |
| --- | --- | --- | --- |
| 你好(3) | 1510.5 / 1.6 | 1297.5 / 1.4 | ❌ 慢 16% |
| 今天天气(14) | 3126.5 / 1.0 | 2887.5 / 0.9 | ❌ 慢 8% |
| 我帮你查(19) | 3554.0 / 1.0 | 3334.5 / 0.9 | ❌ 慢 7% |
| 人工智能(35) | 4418.0 / 0.9 | 4134.5 / 0.9 | ❌ 慢 7% |
| 好的这个(50) | 3337.0 / 1.0 | 4136.5 / 0.9 | ✅ 快 19% |

（n=20，多数 sd < 250ms，结论稳定非随机噪声；首轮 n=5 抽样已呈同向，加大样本后复现。）

### 技术判断（为何 fp16 在此无效）
- Tesla T4 虽有 FP16 Tensor Core，但 **CosyVoice2 推理瓶颈不在可被 fp16 加速的矩阵算子**上，而在自回归 LM 的**串行 token 生成 + 采样**开销；fp16↔fp32 转换反而引入额外开销。
- 纯 fp16 不是该模型在 T4 上的有效优化手段。**真正有效**的是 TensorRT（图优化 / kernel 融合）或 vLLM（LM 部分 PagedAttention / 连续批处理）——均为高改造成本项，作为后续候选，本轮不动。

### 收尾
- 两机统一 `COSYVOICE_FP16=false` 并重建 backend（node1/node2 均 healthy，`cosyvoice_fp16=False` 生效）；`.env.deploy` 改动前均备份为 `.env.deploy.bak.fp16`。
- 压测脚本为临时取证，验证后已从两机 host `/tmp` 与容器内清理。
- **拓扑记录**：本轮实测期间发现 node1/node2 **公网 IP 发生互换**（实例重启所致，hostname/内网 IP 稳定）——node1=<NODE_PUBLIC_HOST>（内网 <NODE1_PRIVATE_IP> / <HOSTNAME>…），node2=<NODE_PUBLIC_HOST>（内网 <NODE2_PRIVATE_IP> / <HOSTNAME>…）。历史文档中的旧公网 IP 为当时快照，以内网 IP + hostname + `node_name` 为准。
