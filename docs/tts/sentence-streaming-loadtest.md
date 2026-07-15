> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 分句流式 TTS — 正式多句压测 + 主技术方案文档补全

## Summary

核心任务（node1 改 `.env.deploy` + 重建镜像 + 一轮 after 压测）已在前序会话完成并经本会话**实测复核**：
node1 `TTS_SENTENCE_STREAM=true`、live backend `sentence_stream=True/max_chars=60`、backend healthy；
node2 无 flag、`NODE_NAME=node2`、healthy；runbook §6.5/§6.5.1 已记录多句 A/B（n=8）TTFB p50 5393→1716ms（↓68%）。

本次「继续」的收尾范围（已与用户确认）：
1. **正式多句压测**：当前 §6.5.1 仅 n=8 临时样本。给 loadtest 脚本加多句语料，对公网 LB 跑更大样本（concurrency=2 total=60 warmup=4），收集客户端 by-node 分位数 + Prometheus by-node 分位数（网络无关），回写 runbook 量化收益。
2. **补主技术方案文档**：把 Phase 3 B 线「分句流式」实施记录补进 `语音大模型后端服务-技术方案与实施计划.md`（§7 优化手段 + §9 M6 里程碑状态）。

**不做**：git 提交（用户未选）。不动 node2（保持基线对照）。不改后端业务代码（已部署生效，无需重建镜像）。

## Current State Analysis

- **后端代码**（已部署）：`backend/app/utils/text_split.py`、`config.py`（`tts_sentence_stream`/`tts_max_sentence_chars`）、`main.py` 接线、`dispatcher.py` `tts_stream` 单 `tts_slot()` 内逐句循环 — 全部确认在位，无需改动。
- **压测脚本** [scripts/tts_stream_loadtest.py](../../scripts/tts_stream_loadtest.py)：
  - `DEFAULT_CORPUS`（line 47-60）6 条**均为单句**（句中只有逗号、以单个 `。` 结尾）→ 这是 §6.5 主表「无改善」的根因。
  - `one_request()`（line 78）/ `_report()`（line 201）已支持客户端 TTFB + **by-node 拆分**（line 236-251），可直接复用。
  - `run()`（line 155）写死 `corpus = DEFAULT_CORPUS`（line 163），需要加一个语料选择入口。
- **Prometheus**：跑在 node1，端口仅绑 127.0.0.1（SSH 上机用 `curl -G --data-urlencode` 查 `tts_ttfb_seconds_bucket` by node）。
- **主技术方案文档** [语音大模型后端服务-技术方案与实施计划.md](../architecture/overview.md)：
  - §7.2「针对性优化手段」（line 335-355）列了 vLLM/量化/TensorRT/流式分块，**未含已落地的「分句流式」**。
  - §7.4 验收标准（line 361-365）有 TTFB 项但无 B 线结果记录。
  - §9 M6（line 394）状态写「Step 1 QoS 已上线；B 线 vLLM 待做」，**未提分句流式已落地**。

## Proposed Changes

### 1. `scripts/tts_stream_loadtest.py` — 加多句语料选项

**What**：新增 `MULTI_SENTENCE_CORPUS`（每条都是多句、首句极短，能触发分句器）+ `--corpus {default,multi}` 参数（默认 `default`，保持现有行为不变）。

**Why**：用与 §6.5.1 同性质但更丰富的多句语料跑正式样本，量化分句流式对真实多句文本的 TTFB 收益；保留 `default` 不破坏既有单句 baseline 复现。

**How**：
- 在 `DEFAULT_CORPUS` 后新增（每条首句短、共 5-6 句，覆盖不同长度）：
  ```python
  # 多句语料: 每条含多个句末标点且首句极短, 用于验证/量化分句流式 TTFB 收益。
  MULTI_SENTENCE_CORPUS = [
      "你好。今天天气很好。我们一起去公园散步吧。路上可以聊聊最近的新闻。然后再找家餐厅吃饭。",
      "好的。这个问题我来解释一下。首先要理解它的背景。其次是具体的实现方式。最后我们看几个例子。",
      "早上好。会议改到下午三点了。地点在二楼会议室。记得带上你的笔记本。我们准时开始。",
      "明白了。我先确认一下需求。然后给出初步方案。如果没问题就进入开发。预计本周内交付。",
      "嗯。这道菜其实很简单。先把食材洗净切好。再热油下锅翻炒。最后调味出锅就可以了。",
      "可以。我来总结今天的要点。第一是进度符合预期。第二是风险已经识别。第三是下一步计划已定。",
  ]
  ```
- `run()` 里 `corpus = DEFAULT_CORPUS`（line 163）改为按 `args.corpus` 选：
  ```python
  corpus = MULTI_SENTENCE_CORPUS if args.corpus == "multi" else DEFAULT_CORPUS
  ```
- `main()` 加参数：
  ```python
  p.add_argument("--corpus", choices=["default", "multi"], default="default",
                 help="default=单句 baseline 语料; multi=多句语料(验证分句流式)")
  ```

### 2. 执行正式多句压测

**命令**（本地，对公网 LB；自签证书 `--insecure` 默认开）：
```bash
cd <repo-root>
.venv-or-backend-venv/bin/python scripts/tts_stream_loadtest.py \
  --url wss://your-domain.example.com/ws/tts --corpus multi \
  --concurrency 2 --total 60 --warmup 4 --verbose
```
（用 `backend/.venv` 的 python，已装 websockets 15.0.1。）

**采集**：
- 客户端报告的 **by-node TTFB p50/p95**（node1 优化版 vs node2 基线版，LB 自动分流）。
- 服务端 Prometheus by-node（SSH 到 node1，10m 窗口，网络无关）：
  ```bash
  ssh -i ~/.ssh/your_key.pem root@your-server.example.com \
    "curl -s -G http://127.0.0.1:9090/api/v1/query \
     --data-urlencode 'query=histogram_quantile(0.5, sum(rate(tts_ttfb_seconds_bucket[10m])) by (le, node))'"
  ```
  p95 同理（0.95）。

### 3. 回写 runbook §6.5.2

**What**：在 [可观测性Runbook-QoS监控栈.md](../observability/qos-monitoring.md) §6.5.1 后新增 **§6.5.2「多句语料正式压测（更大样本）」**。

**Why**：把 §6.5.1 的 n=8 临时结论升级为正式样本 + 服务端 Prometheus 佐证，量化收益落档。

**How**：记录命令、客户端 by-node 表（n/TTFB p50/p95）、Prometheus by-node 表、结论（与 §6.5.1 一致性校验 + 收益绑定首句长度）。如实记录实测数字，不预设结果。

### 4. 补主技术方案文档

**What**：编辑 [语音大模型后端服务-技术方案与实施计划.md](../architecture/overview.md)：
- **§7.2「延迟优化」**（line 348-351）追加一条「**分句流式合成（已落地，node1 灰度）**」：原理（按标点分句、单信号量内逐句流式、首短句先出块把 TTFB 绑定到首句长度）、引擎无关零依赖、默认关、适用边界（多句有效/单句无效）、收益（引用 runbook §6.5.2 实测数字）。
- **§9 M6 状态**（line 394）更新：在「Step 1 QoS 已上线」后补「**B 线分句流式 TTFB 优化已落地（node1 灰度，多句 TTFB p50 ↓~68%）；vLLM 推理优化经评估高风险暂缓（见 runbook §6.3）**」。

**Why**：主文档是项目唯一权威进度视图，目前缺 B 线已落地这一事实，需与 runbook 对齐。

## Assumptions & Decisions

- **样本量 concurrency=2 total=60**：单 GPU 串行化，concurrency 过高只会排队 429；2 并发 + 60 总量在两机间分流后每机约 30 条，足够稳定分位数且不打爆 GPU。沿用 §6.5 同参数便于横比。
- **公网 LB（wss://your-domain.example.com）而非分别打内网**：要让 LB 自然分流到 node1/node2 形成同条件 A/B；客户端 by-node 拆分已能区分两机。Prometheus by-node 作网络无关佐证。
- **多句语料设计**：每条首句 2-3 字（「你好。」「好的。」），确保分句器命中、首句 LLM 段近乎瞬时出块 —— 这正是优化收益来源。
- **不动后端代码/不重建镜像**：优化已部署生效，本次仅压测 + 文档，零生产风险。
- **如实记录**：若正式样本收益与 §6.5.1 不一致（如样本噪声导致差异收窄），按实记录并标注，不粉饰。

## Verification

1. 脚本改动后先本地 `--corpus multi --total 4 --warmup 0 --verbose` 冒烟，确认多句语料被加载、by-node 拆分正常。
2. 正式跑 total=60，确认 node1/node2 均有样本（busy/err 计数合理）。
3. Prometheus by-node 查询返回两机分位数（非空）。
4. runbook §6.5.2 与主文档 §7.2/§9 写入后回读校验，数字与压测输出一致。
5. 复核 node2 全程无 flag（基线对照成立）。
