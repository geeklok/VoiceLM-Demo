> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 可观测性 Runbook（Phase 3 §7.1/7.3 — QoS 指标 + Prometheus/Grafana）

本文档落地 Phase 3 的 **A 线：QoS 指标体系 + 可观测性**。目标是先把 ASR/TTS 的质量与性能指标埋点导出，用 Prometheus 采集、Grafana 出图，拿到 **优化前的 baseline**——这是后续 B 线（vLLM PagedAttention 等推理优化，§7.2）做「前后对比」的前提（验收 §7.4）。

> ⚠️ **安全边界**：backend 无鉴权，8000 只在内网/docker 网络可达，严禁公网暴露。监控栈的 Prometheus(9090)/Grafana(3000) **只绑 `127.0.0.1`**，不进公网安全组，远程访问一律走 SSH 端口转发。

---

## 1. 已落地的埋点（代码层）

### 1.1 指标模块（`backend/app/observability/metrics.py`）

独立 `CollectorRegistry`（避免和第三方库默认 registry 冲突），所有指标经两个 helper 上报，业务代码不直接碰指标对象：

| 指标 | 类型 | labels | 含义 |
|------|------|--------|------|
| `asr_requests_total` | Counter | model, status, degraded | ASR 请求数（区分降级/失败）|
| `asr_rtf` | Histogram | model | ASR 实时率（处理时长/音频时长），越小越好 |
| `asr_process_seconds` | Histogram | model | ASR 单请求推理耗时 |
| `asr_audio_seconds` | Histogram | — | ASR 输入音频时长 |
| `tts_requests_total` | Counter | model, mode, status | TTS 请求数（mode=file/stream）|
| `tts_ttfb_seconds` | Histogram | model | TTS 首包延迟（**仅流式**），CosyVoice2 目标 ~0.15s |
| `tts_rtf` | Histogram | model | TTS 实时率（合成耗时/音频时长），<1 才实时 |
| `tts_process_seconds` | Histogram | model, mode | TTS 单请求合成总耗时 |
| `gpu_inflight` | Gauge | kind(asr/tts) | 当前占用 GPU 信号量的请求数 |
| `gpu_queue_rejections_total` | Counter | kind | 排队超时被拒(429) 数 |
| `inference_failures_total` | Counter | engine | 推理失败次数（触发降级/熔断）|

helper：`observe_asr(model, status=, degraded=, process_ms=, audio_ms=, rtf=)`、`observe_tts(model, mode, status=, process_ms=, ttfb_ms=, rtf=)`。

### 1.2 埋点接入点

- **`orchestration/dispatcher.py`**：ASR 成功/降级/全失败分支调用 `observe_asr`；`tts_file`（file 模式）算 `process_ms`/`rtf` 后 `observe_tts(..., "file", ...)`；`tts_stream`（stream 模式）在首个 chunk 算 `ttfb_ms`、流结束算 `process_ms`/`rtf` 后 `observe_tts(..., "stream", ...)`。失败分支上报 `inference_failures_total` / status=error。
- **`orchestration/limiter.py`**：获取 GPU 信号量许可时 `gpu_inflight.inc()`，finally `dec()`；排队超时分支 `gpu_queue_rejections_total.inc()`。
- **`api/routes_health.py`**：`GET /metrics` 用 `generate_latest(REGISTRY)` 渲染（Prometheus 文本格式）。

> 注意：**TTFB 只有流式(WebSocket `/ws/tts`)模式才有**；`POST /api/v1/tts`（file 模式）上报的是 `tts_process_seconds`，`tts_ttfb_seconds` 不会有数据，这是预期。

### 1.3 依赖

`backend/pyproject.toml` 加了 `prometheus-client>=0.20`（镜像内由 `pip install -e ".[engines]"` 带入；线上实际装的是 0.25.0）。

---

## 2. 监控栈（部署层，仅入口机跑）

### 2.1 文件清单

```
deploy/docker-compose.observability.yml          # Prometheus + Grafana 独立 compose
deploy/observability/prometheus.yml              # 抓取配置 (两机 backend + 自身)
deploy/observability/grafana/provisioning/
    datasources/prometheus.yml                   # 数据源 (uid=prometheus, 默认)
    dashboards/provider.yml                       # 看板 file provider
deploy/observability/grafana/dashboards/
    asr-tts-qos.json                             # QoS 看板 (uid=asr-tts-qos)
```

### 2.2 架构

监控栈作为**独立 compose** 起在入口机，以 `external` 方式接入主栈的 docker 网络 `deploy_default`：

- 抓 **node1**：靠 docker DNS 解析服务名 `backend:8000`（与 nginx 同网络）。
- 抓 **node2**：经 VPC 内网 `<NODE2_PRIVATE_HOST>:8000`（node2 override 把容器 8000 发布到宿主机内网 IP）。
- Prometheus/Grafana 端口都绑 `127.0.0.1`，宿主机外不可直连。

```
                         入口机 (<PUBLIC_HOST> / 内网 <NODE1_PRIVATE_IP>)
   ┌─────────────────────────────────────────────────────────────┐
   │  deploy_default (docker bridge)                               │
   │   ┌──────────┐   ┌──────────┐   ┌────────────┐  ┌─────────┐  │
   │   │ web      │   │ backend  │◄──┤ prometheus │  │ grafana │  │
   │   │ (nginx)  │   │ :8000    │   │  :9090     │◄─┤  :3000  │  │
   │   └──────────┘   └──────────┘   └─────┬──────┘  └─────────┘  │
   │      127.0.0.1:9090 / :3000  ◄────────┘ (仅本机)             │
   └────────────────────────────────────┼────────────────────────┘
                                         │ VPC 内网抓取
                                         ▼
                          node2 <NODE2_PRIVATE_HOST>:8000/metrics
```

### 2.3 启动 / 停止

```bash
cd /opt/asr/deploy

# 启动 (镜像 prom/prometheus:v2.54.1 + grafana/grafana:11.2.0, 走 dockerhub)
docker compose -f docker-compose.observability.yml up -d

# 状态
docker ps | grep -E "prometheus|grafana"

# 停止 (不删数据卷)
docker compose -f docker-compose.observability.yml down

# 彻底清掉含历史数据
docker compose -f docker-compose.observability.yml down -v
```

数据持久化在 named volume `prometheus_data`（保留 15d）/ `grafana_data`。

### 2.4 远程访问（SSH 端口转发，不开公网）

本地机器执行，把入口机的 3000/9090 转发到本地：

```bash
ssh -i ~/.ssh/your_key.pem \
    -L 3000:127.0.0.1:3000 \
    -L 9090:127.0.0.1:9090 \
    root@your-server.example.com
```

然后浏览器开：
- Grafana：http://localhost:3000 （初始 admin/admin，**首次登录请改密码**）
- Prometheus：http://localhost:9090

看板：Grafana → Dashboards → **ASR / TTS QoS**（uid `asr-tts-qos`）。

---

## 3. 上线实测记录（2026-06-27）

Phase 3 Step 1 已在双机集群上线并端到端验证通过。

**部署动作：**
- 入口机 backend 重建带 `/metrics`（image `7049c9c3bb49`），`/readyz` ready、`/metrics` 1457B 含全部 QoS 指标。
- node2 因 **dockerhub 被墙**（拉 `nvidia/cuda` base `connection refused`）无法本地 build，沿用扩容时的内网直传：入口机 `docker save asr-tts-backend:latest | ssh -i /tmp/xfer_key root@<NODE2_PRIVATE_IP> 'docker load'`（~2.5 分钟），两机 image ID 对齐 `<IMAGE_ID>`；node2 `docker compose up -d backend`，~40s 后 ready、`/metrics` live。临时互信密钥用完即从两机清除。
- 入口机起监控栈：Prometheus/Grafana 容器 Up，端口均绑 `127.0.0.1`。

**端到端验证：**

| 检查项 | 结果 |
|--------|------|
| Prometheus targets | node1 / node2 / prometheus 三个全部 `up` |
| 打流（经公网 LB，各 6 ASR + 6 TTS） | LB 均衡：两机各 3 ASR + 3 TTS |
| `asr_rtf` | 两机各 3 样本，sum≈0.088/0.090 → **均值 RTF ~0.029**（T4 上 SenseVoice 极快）|
| `tts_process_seconds`（file 模式） | 两机各 3 样本；TTFB 为空（file 模式无首包指标，预期）|
| Grafana 健康 / 数据源 | health ok，datasource `prometheus` 默认，看板 `asr-tts-qos` 已加载 |
| Grafana→Prometheus 代理查询 | `sum(asr_requests_total)`=6，成功出数 |

> baseline 已建立：file 模式 ASR/TTS 指标在 Grafana 可见。**TTS TTFB / RTF / 流式平滑度需走流式接口(`/ws/tts`)才会有数据**——后续 B 线对比优化效果时，应专门跑流式压测填充 `tts_ttfb_seconds`。

---

## 4. 常用 PromQL（排查 / 看 baseline）

```promql
# ASR p95 实时率 (按机器)
histogram_quantile(0.95, sum(rate(asr_rtf_bucket[5m])) by (le, node))

# ASR p95 推理耗时
histogram_quantile(0.95, sum(rate(asr_process_seconds_bucket[5m])) by (le))

# ASR 错误率
sum(rate(asr_requests_total{status="error"}[5m])) / sum(rate(asr_requests_total[5m]))

# TTS 首包 p95 (流式)
histogram_quantile(0.95, sum(rate(tts_ttfb_seconds_bucket[5m])) by (le))

# 当前 GPU inflight
gpu_inflight

# 限流拒绝 / 推理失败速率
sum(rate(gpu_queue_rejections_total[5m])) by (kind)
sum(rate(inference_failures_total[5m])) by (engine)
```

---

## 5. 常见问题

**Q1. Grafana 看板某些图 "No data"？**
- TTS TTFB/RTF 面板：file 模式没有 TTFB，需跑流式(`/ws/tts`)。RTF 仅成功请求且能算出音频时长时才有。
- 直方图分位数刚起步样本太少会出 `NaN`，多打点 + 等 1~2 个 scrape 周期。

**Q2. Prometheus target node2 DOWN？**
- 检查 node2 backend 是否 ready、override 是否把 8000 发布到 `<NODE2_PRIVATE_IP>`、安全组是否放行来源=入口机的 8000。
- 入口机 `curl http://<NODE2_PRIVATE_HOST>:8000/metrics`（应在容器/宿主内网可达）。

**Q3. 改了 `prometheus.yml` 不生效？**
- `docker compose -f docker-compose.observability.yml restart prometheus`，或 `curl -X POST http://127.0.0.1:9090/-/reload`（需启用 lifecycle，当前未启用，用 restart）。

**Q4. 改了看板 JSON 不更新？**
- file provider `updateIntervalSeconds=30`，等 30s 或 `docker restart deploy-grafana-1`。注意 provisioning 的看板在 UI 上是只读的，改动要落回 `dashboards/asr-tts-qos.json`。

**Q5. node2 重新 build 失败（dockerhub 被墙）？**
- 不要在 node2 本地 build。改由入口机 `docker save | ssh | docker load` 内网直传（见 §3 与《水平扩容Runbook》§3.5）。

---

## 6. B 线灰度对比：节点维度透出 + before baseline

### 6.1 设计

B 线（vLLM 等推理优化）**只应用在入口机 node1，扩展机 node2 保持旧版**，形成「版本灰度 + QoS 对比」。为此打通了节点维度的全链路透出：

- **后端**：`Settings.node_name`（env `NODE_NAME` 注入，node1/node2），随请求回显。ASR REST → `ASRResponse.node`；TTS file → 响应头 `X-Node/X-Model/X-Process-Ms/X-Audio-Ms/X-RTF`；TTS WS → meta 帧（node/model）+ done 帧（qos）；ASR WS → partial/final 的 node 字段。
- **前端**：`QosBadge` 组件展示本次请求落到哪台机器（node1 绿=优化版 / node2 橙=基线版）+ 首包/处理/RTF。TtsPage、AsrPage 均接入。
- **看板**：`asr-tts-qos.json` 顶部新增「灰度对比」行，TTFB/RTF/请求速率全部 `by(node)` 并排。
- **Prometheus**：scrape target 注入 `node=node1/node2` label（见 `prometheus.yml`）。

> ⚠️ **部署坑**：`sync.sh` 的 `rsync --delete` 会误删 node2 专属的 `docker-compose.override.yml`（该文件不在仓库里）。已在 sync.sh 加 `--exclude=docker-compose.override.yml`。该 override 把 node2 backend 8000 **仅绑 VPC 内网** `<NODE2_PRIVATE_HOST>:8000`（非 0.0.0.0），公网不可达。

### 6.2 before baseline（优化前，两机版本一致）

部署「节点透出」后、node1 打开 vLLM **之前**，跑顺序压测（`concurrency=1`，无 GPU 争抢，数据干净）：

```bash
cd backend && .venv/bin/python ../scripts/tts_stream_loadtest.py \
    --url wss://your-domain.example.com/ws/tts --concurrency 1 --total 24 --warmup 2
```

服务端 Prometheus（网络无关，真实 baseline）`tts_ttfb_seconds` by node：

| 指标 | node1（待优化） | node2（基线） |
|------|------|------|
| TTFB p50 | ~3.8s | ~3.4s |
| TTFB p95 | ~4.9s | ~4.8s |
| TTFB p99 | ~5.0s | ~5.0s |
| RTF p50 | ~0.92 | ~0.96 |
| RTF p95 | ~1.34 | ~1.94 |

> **关键结论**：当前两机 TTFB 接近（均 ~4-5s，远未达 150ms 目标），证明灰度基础设施正确（同版本无系统性差异）。这就是 B 线优化的 **before 锚点**——node1 打开 vLLM 后，其 TTFB 应显著下降，node2 维持本表数值，两线差距即优化收益。客户端侧（含网络往返）TTFB 略大，p50≈4.8s，趋势一致。
>
> 注：TTFB 随文本长度增长（len=3→~1.7s，len=89→~5s），CosyVoice2 首包随 prompt 编码量上升，是 vLLM PagedAttention 要解决的核心 gap。

### 6.3 vLLM 可行性调研结论（不采用，留档）

打开 `COSYVOICE_LOAD_VLLM=true` 在本环境**不是干净的开关切换**，调研结论如下（2026-06-28）：

- flag 接线本身通：`config.cosyvoice_load_vllm` → `CosyVoiceEngine._load()` → `CosyVoice2(load_vllm=...)`。
- 但 CosyVoice 的 vLLM 路径（`cosyvoice/cli/model.py:load_vllm`）用到 `enable_prompt_embeds=True` / `skip_tokenizer_init=True`，**最低要 vllm 0.9.0**（其 README 明示 <0.9.0 不支持、0.10.x 未测试）。
- vllm 0.9.0 **硬依赖 torch 2.7.0+**，而镜像当前 pin 在 **torch 2.3.1+cu121**（CosyVoice requirements 也 pin `torch==2.3.1`）。打开 vLLM 等于一次 torch 2.3→2.7 大版本升级 + 连带 torchaudio/transformers 升级，**有打破入口机已跑通 ASR(FunASR)+TTS 全栈的真实风险**。
- **T4 = 计算能力 7.5，不支持 bf16**；CosyVoice 的 `EngineArgs` 未指定 dtype，需补丁强制 `dtype="float16"` 才能在 T4 起来。
- 非瓶颈项：显存够（剩 ~10GB，vLLM `gpu_memory_utilization=0.2`≈3GB）、磁盘够（剩 51GB）、驱动够（535 / CUDA 12.2）。

**决策**：blast radius 太大且与「全量已跑通」现状对赌，暂不采用 vLLM。若将来要做，应单独构建一个 vllm 镜像变体（torch 2.7 + vllm 0.9.0 + 补丁 fp16），离线验证稳定后再灰度到 node1，绝不在生产镜像上原地升级。

### 6.4 after 对比（已执行：node1 开 JIT，低风险路线）

vLLM 既然高风险，改用**零装包、零重建**的 JIT 加速做 after：node1 模型目录已存在 `flow.encoder.fp16.zip`，且 `COSYVOICE_FP16=true`，只需 `.env.deploy` 加 `COSYVOICE_LOAD_JIT=true` + `docker compose up -d backend` 重建容器即可（node2 不动）。

```bash
# node1 /opt/asr/deploy/.env.deploy
COSYVOICE_LOAD_JIT=true        # 加在 COSYVOICE_LOAD_VLLM=false 之后
# 重建容器拾取新 env (无需 rebuild 镜像)
cd /opt/asr/deploy && docker compose up -d backend
```

同样 `concurrency=1 --total 24 --warmup 2` 顺序压测，服务端 Prometheus `tts_ttfb_seconds` / `tts_rtf` by node（网络无关）：

| 指标 | node1 before（无 JIT） | node1 after（JIT） | node2（对照，全程基线） |
|------|------|------|------|
| TTFB p50 | 3.82s | **4.10s** | 3.375s（前后不变）|
| TTFB p95 | ~4.9s | 10.0s* | 4.84s |
| RTF p50 | ~0.92 | **0.918** | 0.96 → 0.986 |
| TTS process p50 | — | 9.17s | 5.83s |

> **关键结论（如实记录）**：**JIT 在本场景未带来可测量的 TTFB/RTF 改善**（node1 TTFB 3.82→4.10、RTF 0.92→0.918，差异在小样本噪声范围内）。对照组 node2 全程未动、数值稳定（TTFB 3.375s），证明测量方法可靠、灰度基础设施正确。
>
> 原因分析：TTS 的 TTFB 瓶颈在 **LLM 自回归生成首个语音 token**（CosyVoice2 的 Qwen2 LLM 段），而 JIT 只加速 `flow.encoder`（声学解码段），不在首包关键路径上——这正是 vLLM（PagedAttention 加速 LLM 段）本来要解决的部分。所以 JIT 对 TTFB 无效符合预期。
>
> *p95=10.0s 为直方图 bucket 封顶饱和（含一条 16.2s 离群值），n=12 小样本噪声大，不作为结论依据。

**下一步选项**：① 真正要压 TTFB 必须动 LLM 段（vLLM，见 §6.3 高风险路线，需离线镜像验证）；② 或换思路——短文本 TTFB 已 ~1.6s（len=3），长文本才涨到 ~5s，可在应用层做**文本分句 + 流式拼接**，让首句快速返回，绕开单次长 prompt 的首包惩罚（零依赖、零风险，纯后端逻辑）。

> **决策（2026-06-28）**：JIT 实测对 TTFB 无益，已**回滚** node1 的 `COSYVOICE_LOAD_JIT`（从 `.env.deploy` 删除并重建 backend，readyz ok），两机回到完全一致的干净基线。下一步采用上面的选项②「分句流式」做真正的 TTFB 优化。

### 6.5 after 对比（已执行：node1 开「分句流式」，纯后端逻辑路线）

按选项② 实现「文本分句 + 流式拼接」：后端新增 `app/utils/text_split.py`（`split_sentences`，按中英文主标点切句、超长句按次标点/硬截断到 ≤60 字），`config.py` 加 `tts_sentence_stream`/`tts_max_sentence_chars` 两个 flag，`dispatcher.py` 的 `tts_stream` 在**单个 `tts_slot()` 内逐句**调 `synthesize_stream`（保持 ttfb_ms/qos_holder/done 帧契约不变）。默认关，仅 node1 灰度打开（`TTS_SENTENCE_STREAM=true`），node2 保持基线。本地 `pytest` 40 passed（含新增分句单测 + WS 回归）。

部署：rsync `backend/app` → node1 `/opt/asr`，`.env.deploy` 加 flag（已备份），`docker compose build backend && up -d backend`，readyz ok，容器内确认 `sentence_stream=True`。

`--concurrency 2 --total 60 --warmup 4` 打公网 LB，服务端 Prometheus `tts_ttfb_seconds` by node（10m 窗口，网络无关）：

| 指标 | node1 baseline | node1 after（分句流式） | node2（对照，全程基线） |
|------|------|------|------|
| TTFB p50 | 3.82s | **3.667s** | 3.708s |
| TTFB p95 | ~4.9s | 4.867s | 5.375s |
| RTF p50（客户端含网络） | ~0.92 | 0.929 | 0.987 |

> **关键结论（如实记录）**：**分句流式在本次压测语料下未带来显著 TTFB 改善**（node1 3.82→3.667s，与 node2 3.708s 基本持平，差异在小样本噪声范围内）。
>
> 根因（已用 `split_sentences` 复算压测语料验证）：**压测语料的每条文本恰好是「单句」**——都以单个句号 `。` 结尾，句中只有逗号。分句器按设计只在**句末标点**切句、逗号仅在**整句超 60 字**时才用于二次切短，因此 6 条语料里只有最长一条（72 字）被切成 3 句，其余 5 条都被当作 1 句整体合成 → 等价于基线，TTFB 自然没变。
>
> 也就是说：优化逻辑本身正确（单测已证多句文本会被正确拆分、首句先返回），但**对「单长句」无能为力**——单句的首包惩罚来自 LLM 段对整句 prompt 的处理，分句无法切入句内。这与 §6.4 的结论一致：要压「单句」TTFB 仍须动 LLM 段（vLLM）。
>
> **适用边界**：分句流式对**多句长文本**（含句号/问号/感叹号的段落）有效——首句短、先出声，体感 TTFB 取决于首句而非全文；对**单句**（无句末标点的一整句）无效。真实对话/朗读场景多为多句，故保留该优化在 node1（低风险、默认可关），但需用**多句语料**重测才能量化收益。

#### 6.5.1 多句语料补测（决定性 A/B，已执行）

用一条**真·多句**文本（首句极短）对 node1/node2 各打 8 次，客户端测 TTFB（含网络往返，两机同条件可直接相比）：

```
文本: 你好。今天天气很好。我们一起去公园散步吧。路上可以聊聊最近的新闻。然后再找家餐厅吃饭。
```

| 指标（客户端 TTFB） | node1（分句流式） | node2（基线） | 改善 |
|------|------|------|------|
| p50 | **1716ms** | 5393ms | **↓ 68%** |
| min | 1474ms | 4808ms | — |
| max | 2572ms | 14196ms | — |

> **决定性结论**：多句文本下分句流式**显著降低 TTFB**（p50 5.4s → 1.7s，约 -68%）。node1 首句很短（「你好。」），LLM 段几乎瞬时出第一个语音 token，首块随即返回；node2 必须等整段 prompt 处理完才出首块。这验证了优化逻辑完全正确——§6.5 主表「无改善」纯粹是因为默认压测语料是**单句**、分句器按设计未触发，并非优化失效。
>
> 收益绑定**首句长度**而非全文长度：文本越长、句子越多，分句流式相对基线的 TTFB 优势越大。

> **决策（2026-06-28）**：分句流式逻辑正确且零风险，**保留在 node1 灰度**（`TTS_SENTENCE_STREAM=true`）。本轮用单句语料未能体现收益，待用多句语料补测；node2 维持基线。代码改动已在本地 40 passed，node1 已部署生效。

#### 6.5.2 多句语料正式压测（更大样本 + Prometheus 佐证，已执行）

把 §6.5.1 的 n=8 临时结论升级为正式样本：给 `scripts/tts_stream_loadtest.py` 加 `MULTI_SENTENCE_CORPUS`（6 条多句、每条首句极短）+ `--corpus multi` 开关（默认 `default` 不变），对公网 LB 跑 `--concurrency 2 --total 60 --warmup 4`，让 LB 自然分流形成 node1（分句流式）vs node2（基线）同条件 A/B。

```bash
# 本地 (../../backend/.venv, websockets 15.0.1)
python scripts/tts_stream_loadtest.py --url wss://your-domain.example.com/ws/tts \
    --corpus multi --concurrency 2 --total 60 --warmup 4
```

**客户端测量（含网络往返，60/60 成功，0 限流 0 失败）**：

| 指标（客户端 TTFB） | node1（分句流式，n=26） | node2（基线，n=34） | 改善 |
|------|------|------|------|
| p50 | **1812ms** | 4945ms | **↓ 63%** |
| p95 | **2742ms** | 5960ms | ↓ 54% |
| RTF p50 | 1.155 | 0.929 | —（含网络，不可比）|

**服务端 Prometheus by-node（网络无关，10m 窗口）**：

```bash
ssh -i ~/.ssh/your_key.pem root@your-server.example.com \
  "curl -s -G http://127.0.0.1:9090/api/v1/query \
   --data-urlencode 'query=histogram_quantile(0.5, sum(rate(tts_ttfb_seconds_bucket[10m])) by (le, node))'"
```

| 指标（Prometheus TTFB） | node1（分句流式） | node2（基线） | 改善 |
|------|------|------|------|
| p50 | **1.5s** | 4.0s | **↓ 62.5%** |
| p95 | **1.95s** | 4.9s | ↓ 60% |

> **结论（正式样本确认）**：多句长文本下分句流式把 TTFB **降约 60-63%**（客户端 p50 4945→1812ms；服务端 p50 4.0→1.5s）。客户端与服务端两套独立测量、node1/node2 两机对照三向印证，与 §6.5.1 的 n=8 结论（↓68%）方向一致（量值差异源于本轮语料更丰富、句长分布更均衡，且样本量更大噪声更低）。
>
> 收益绑定**首句长度**而非全文：node1 首句极短（「你好。」「好的。」），LLM 段近乎瞬时出首块；node2 须等整段 prompt 处理完才出首块。RTF 差异属客户端含网络往返的伪差异（服务端 RTF 两机相当），不作结论。
>
> **最终决策**：分句流式为低风险、引擎无关、默认可关的有效 TTFB 优化，**保留 node1 灰度**（`TTS_SENTENCE_STREAM=true`），node2 维持基线对照。本优化为 T4 上不动 LLM 段（vLLM 高风险，见 §6.3）即可拿到的 TTFB 收益。已同步至主技术方案文档 §7.2 / §9 M6。

#### 6.5.3 句间间隔修复：最小段长贪心合并（已落地，node1 灰度）

**问题（前端实测反馈）**：分句流式上线后首包确实变快，但当**句与句长度差较大**时，语音播放在句间出现明显间隔，体感卡顿。

**根因**：单 GPU 串行下，每句各跑一遍 LLM prefill（处理该句 prompt 才能出第一个语音 token，约 1.5s）。前端播放器 [player.ts](../../frontend/src/audio/player.ts) 用 Web Audio 无缝排程（`nextTime` 累加）但**无 jitter buffer**；若首句音频时长盖不住下一句的 prefill 时间，播放器欠载 → 句间空档。**句子越短、下一句越长，间隔越明显**（如「你好。」只有 ~0.5s 音频，却要等下句 ~1.5s prefill）。GPU 串行无法靠并行消除。

**修复（方案 A：后端最小段长贪心合并）**：在 [text_split.py](../../backend/app/utils/text_split.py) 的 `split_sentences` 加第三参 `min_chars`；分句+capping 后调 `_merge_short` 把过短段**向后贪心合并到 `≥min_chars`**（但不超过 `max_chars`，末尾孤短段并入前段）。把极短碎句（如「你好。」）并掉，避免首段音频盖不住下段 prefill 造成的句间空档。代价是首包略慢（用一点 TTFB 换播放平滑）。`min_chars=0`（默认）不合并 = 保持 §6.5.2 现状。

接线：`config.py` 新增 `tts_min_sentence_chars`（默认 0）→ `main.py` 进 Dispatcher 构造 → `dispatcher.py` 的 `tts_stream` 调三参 `split_sentences`。纯应用层逻辑，零依赖零模型改动，引擎无关。

```bash
# node1 启用 (../../deploy/.env.deploy 追加, 已备份原文件)
TTS_MIN_SENTENCE_CHARS=5
# 首次需重建 backend 镜像 (代码打进镜像); 后续仅调 env 值用 up -d 重新加载即可
cd /opt/asr/deploy && docker compose build backend && docker compose up -d backend
# 纯 env 调值 (代码已在镜像内):
cd /opt/asr/deploy && docker compose up -d backend
```

**验证**：本地 45 passed（test_text_split.py 新增 4 例 min_chars 行为 + test_dispatcher.py 新增 1 例合并验证）。node1 live config 确认 `sentence_stream=True max=60 min=5 node=node1`，容器 healthy。

**min_chars 调参经过（2026-06-28，单 GPU 串行下首包与句间平滑此消彼长，用 `min_chars` 调是零和）**：

| min_chars | node1 TTFB p50（客户端，含网络） | 取舍 |
|------|------|------|
| 0 | 1812ms（见 §6.5.2） | 首包最快，但句长差大时句间间隔明显 |
| 20 | — | 句间最平滑，但首包被撑长（碎句并到 ~20 字） |
| 8 | 3445ms | 首包仍偏慢（碎句并到 ~8 字） |
| **5（当前）** | **3245ms** | 折中：并掉 2-3 字极短碎句，首包略优于 8 |

> 同批多句压测（`--corpus multi --concurrency 2 --total 40`）下 node1（min=5）vs node2（基线，未开分句流式）：node1 TTFB p50 **3245ms** / p95 3922ms，node2 p50 4756ms / p95 5222ms，node1 相对基线 **↓约 32%**（p95 ↓25%）。结论：相对基线 node1 明确更快这一点稳定；但 min=5 的绝对值（3245ms）仍高于 min=0 的 1812ms——合并任何碎句都会拉长首段 prefill，这是 `min_chars>0` 的固有代价。

> **决策（2026-06-28）**：句间间隔修复（最小段长贪心合并）为低风险、默认可关的播放平滑优化，**保留 node1 灰度**（`TTS_MIN_SENTENCE_CHARS=5`，经 20→8→5 微调后定值），node2 维持基线对照（无此 flag，分句也未开）。前端体感「一半快一半慢」主要来自 LB 把约半数请求分到 node2 基线机（未开分句流式），与 node1 的 min 值无关；如需消除，可考虑分句流式灰度转全量（开到 node2）或前端 jitter buffer（见 §7.2 延迟优化）。若仍嫌首包慢可降到 0（放弃句间平滑），若句间又卡可上调。
