> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# T4 显存限制下 vLLM 可行性评估报告

> 数据快照：2026-07-05 | 实测节点：node1（Tesla T4 15360 MiB / 内网 <NODE1_PRIVATE_IP>）
> 结论先行：**当前不建议在单 T4 上引入 vLLM。** 空闲显存（~9GB）表面够放 CosyVoice2-0.5B LLM（fp16 ~1GB 权重），但 vLLM 的**显存预分配模型**（`gpu_memory_utilization` 按总显存比例吞占 KV cache 池）与现有 ASR+TTS 常驻显存**强冲突**，且 T4(SM7.5) 上 vLLM 存在 bf16 不支持、版本链断裂等硬约束。收益（LLM token 生成加速）虽命中 TTS 真瓶颈，但落地风险与运维成本远超单 T4 承载力。

---

## 1. 实测显存基线（地基数据）

### 1.1 GPU 与稳态占用
| 指标 | 值 | 来源 |
| --- | --- | --- |
| GPU 型号 | Tesla T4 | `nvidia-smi` |
| 总显存 | **15360 MiB（≈15.0 GB，非 16GB）** | `nvidia-smi` |
| ASR+TTS 全加载预热稳态 | **5869 MiB** | 单进程 9681 占 5864 MiB |
| 空闲 | **9061 MiB** | — |
| 并发 TTS 推理峰值 | **6074 MiB**（较稳态 +~200 MiB） | 3 路并发 `/api/v1/tts` 实测 |

**关键**：权重常驻，推理临时增量小（~200 MiB）；真实可用余量以**稳态 6.1GB 占用 → 峰值余量约 9GB** 计。

### 1.2 已加载模型清单（5.87GB 稳态的构成）
当前单进程同时常驻以下引擎（权重目录大小为显存下界近似）：

| 模块 | 模型 | 权重大小 | 用途 |
| --- | --- | --- | --- |
| ASR-1 | SenseVoiceSmall | 897 MB | 聊天/文件定稿（多语种+事件标签） |
| ASR-2 | paraformer-online | 849 MB | 真流式第一遍出字 |
| ASR-3 | seaco-paraformer | 953 MB | 热词定稿 |
| ASR-4 | fsmn-vad | 3.9 MB | 端点检测 |
| ASR-5 | punc-ct-transformer | 283 MB | 标点恢复 |
| TTS | **CosyVoice2-0.5B** | **5.3 GB**（llm.pt 2.02G + flow 0.45G×2 + hift 83M） | 语音合成 |

> 注：权重磁盘合计 ~9.1GB，但显存稳态仅 5.87GB——因 fp32 权重按需上显存、部分模块共享/惰性加载。CosyVoice2 的 `llm.pt`（2.02GB fp32）是 vLLM 潜在接管对象。

### 1.3 软件栈约束
| 项 | 值 | 对 vLLM 的影响 |
| --- | --- | --- |
| torch | **2.3.1+cu121** | vLLM 新版要求 torch≥2.4/2.5，需大版本升级 |
| CUDA | 12.1 | 与 vLLM 预编译 wheel 的 CUDA 版本需匹配 |
| 算力 | **SM 7.5（Turing）** | **不支持 bf16**；vLLM 默认 dtype 需强制 fp16，部分 kernel（FlashAttention2/3）在 Turing 受限 |
| vllm | **未安装** | 需新增重依赖（含其自带 torch 版本，易与现有栈冲突） |

---

## 2. vLLM 显存模型：为何与现状强冲突

vLLM 的核心是 **PagedAttention + 预分配 KV cache 池**。它启动时按 `gpu_memory_utilization`（默认 0.9）**一次性吞占该比例的总显存**用作权重 + KV cache 块池，而非按需增长。

在独占 GPU 场景这是优势；但本服务是**多模型共享单 T4**，冲突点：

1. **静态余量不等于 vLLM 可用**：现有 ASR+TTS 已常驻 5.87GB。vLLM 若按默认 0.9×15GB=13.5GB 预占 → **直接 OOM**。必须手动压到 `gpu_memory_utilization ≈ 0.2`（~3GB），留给权重(fp16 ~1GB)+极小 KV cache。
2. **KV cache 池被压缩到接近无效**：3GB 减去 CosyVoice2 LLM fp16 权重 ~1GB，KV cache 仅剩 ~1.5-2GB。CosyVoice2 的 speech-token 序列虽短，但连续批处理（vLLM 的吞吐优势来源）需要足够块池，**池太小 → 退化为近似串行，vLLM 优势基本抹平**。
3. **显存碎片与争抢**：ASR 五模型 + CosyVoice flow/hift 仍在同卡跑推理，与 vLLM 的固定池争剩余显存，峰值叠加易触发 OOM 或 CUDA 分配失败。

### 2.1 显存预算测算（乐观/悲观）
| 分配项 | 乐观（fp16+激进压缩） | 悲观（含碎片+峰值） |
| --- | --- | --- |
| ASR×5 常驻 | 2.5 GB | 3.0 GB |
| CosyVoice flow+hift+其余 | 2.5 GB | 3.0 GB |
| vLLM 权重（CosyVoice2 LLM fp16） | 1.0 GB | 1.2 GB |
| vLLM KV cache 池 | 1.5 GB | 0.8 GB |
| 推理峰值/碎片余量 | 1.5 GB | 1.0 GB |
| **合计** | **9.0 GB（勉强放下）** | **9.0 GB（超 15GB 边界风险）** |

结论：**理论上"塞得下"，但 KV cache 被压到 0.8-1.5GB，vLLM 吞吐优势基本丧失，且峰值 OOM 风险高。**

---

## 3. 收益 vs 成本

### 3.1 收益（命中真瓶颈，但被显存约束抵消）
- fp16 灰度实测已证明 TTS 瓶颈在 **CosyVoice2 LLM 自回归 token 生成**（串行），vLLM 正是针对此（连续批处理 + PagedAttention）。
- **但**：单 T4 上 KV cache 池被压到无效尺寸 → 无法形成有效批处理 → 收益大打折扣。vLLM 的优势在**高并发多请求**共享 KV 池；本服务单卡低并发场景，收益天花板本就有限。

### 3.2 成本（高）
| 成本项 | 说明 | 量级 |
| --- | --- | --- |
| torch 升级 | 2.3.1 → ≥2.4/2.5，牵动 funasr/cosyvoice 全栈兼容性重验 | 高 |
| vLLM 依赖 | 重量级安装，含 CUDA kernel，镜像显著膨胀 | 高 |
| Turing 适配 | SM7.5 不支持 bf16，需强制 fp16 + 规避不支持的 attention kernel | 中高 |
| CosyVoice2 vLLM 导出 | 需 `export_cosyvoice2_vllm`（源码已有），但与我们的加载路径集成需改造 | 中 |
| node2 部署 | node2 Docker Hub 不可达，vLLM 装包非 overlay 增量能覆盖，部署路径要重构 | 高 |
| 调参风险 | `gpu_memory_utilization` 需精调，边界窄，易 OOM；回归验证成本高 | 高 |

---

## 4. 结论与建议

### 4.1 结论
**单 Tesla T4 15GB 上引入 vLLM 加速 CosyVoice2 LLM：技术上勉强可塞，但不推荐。**
- 显存是硬约束：5.87GB 已被 ASR+TTS 常驻，vLLM 的预分配 KV 池模型与之强冲突，压缩后吞吐优势基本丧失。
- 软件栈风险高：torch 大版本升级 + Turing bf16 不支持 + node2 部署路径重构，投入产出比差。
- 收益场景错配：vLLM 优势在高并发批处理；本服务单卡低并发，天花板有限。

### 4.2 替代建议（按 ROI 排序）
1. **零成本 TTFB 优化优先**：对话首句"从短切"（首句用更小 `tts_max_sentence_chars`）+ ASR 聚合窗 600→400ms——直接压首声，无部署/显存成本。
2. **若确需 LLM 加速**：先考虑**独立 GPU 机器专跑 vLLM**（把 CosyVoice2 LLM 拆到独占卡），而非挤单 T4；或换更小/更快的 TTS。这涉及**购买云资源（加 GPU 机器）**，需你决策且产生费用，AI 不代购。
3. **TRT（flow decoder）**：命中吞吐而非 TTFB，成本中高，见《分句流式TTS-TTFB优化方案》fp16 附录后的分析——同样不作为快速开关。

### 4.3 待你决策
- 是否接受"单 T4 不上 vLLM"的结论并归档；
- 若要继续探索 LLM 加速，是否评估**加独立 GPU 机器**（涉及采购成本）。

---

## 5. 量化方案能否替代 vLLM 优化 TTS 延迟（2026-07-05 增补评估）

> 一句话结论：**量化不能有效降低本服务的 TTS 延迟，不推荐用于此目的。** 根因——量化解决的是「显存不足」和「访存带宽受限的大模型」，而本服务的 T4 有 ~9GB 空闲（不缺显存），且瓶颈模型是**极小的 Qwen2-0.5B 级 LLM**，量化在小模型 + batch=1 场景对延迟几乎无正收益，反而可能因反量化开销变慢。

### 5.1 实测：瓶颈模型有多小（决定量化无效的关键）
CosyVoice2 的 LLM 骨架是标准 `Qwen2ForCausalLM`（实测 `cosyvoice/llm/llm.py` L24/L229），config：

| 参数 | 值 | 含义 |
| --- | --- | --- |
| hidden_size | **896** | 极小隐层 |
| num_hidden_layers | **24** | 层数不多 |
| num_attention_heads / kv_heads | 14 / **2**（GQA） | KV 已很省 |
| torch_dtype | **bfloat16** | 原生 bf16 训练 |
| 权重 llm.pt | **2.02 GB（fp32）→ fp16 约 1GB** | Qwen2-0.5B 级 |

这是一个 **0.5B 级模型**。量化的收益公式在这里几乎不成立（见下）。

### 5.2 为什么量化命中不了延迟瓶颈
| 量化的适用前提 | 本服务是否满足 | 说明 |
| --- | --- | --- |
| 显存装不下（需压权重） | ❌ 不满足 | T4 空闲 ~9GB，fp16 LLM 仅 ~1GB，**根本不缺显存** |
| 大模型访存带宽瓶颈（权重搬运主导） | ❌ 不满足 | 0.5B 权重搬运量极小，延迟主导项是**逐 token 的串行计算 + 采样 + Python 调度**，非权重访存 |
| 高并发下靠低精度提吞吐 | ❌ 不满足 | 单 T4 低并发，batch=1 |
| Turing(SM7.5) 有高效 INT8/INT4 kernel | ⚠️ 部分 | T4 有 INT8 Tensor Core，但小模型 + batch=1 难以喂满，收益被反量化(dequant)开销抵消 |

**核心机理**：自回归 TTS 的 TTFB/RTF 瓶颈是「串行生成 N 个 speech token」，每步都是 batch=1 的小矩阵运算。量化（INT8/INT4）主要压**权重访存带宽**——对 7B/14B 大模型有效；但对 0.5B 模型，单步计算本就轻，反量化(INT→FP)的额外算子开销反而可能让**每 token 更慢**。fp16 灰度已实测「多数场景更慢」，量化在同一机理下大概率**同样负面甚至更差**。

### 5.3 各量化路线具体评估
| 路线 | 能否跑 | 延迟收益 | 成本/风险 | 判断 |
| --- | --- | --- | --- | --- |
| **GPTQ/AWQ（INT4 权重量化）** | Qwen2 生态支持 | 🔴 负/无（小模型 batch=1，dequant 开销主导） | 需量化校准 + 集成到 CosyVoice 加载路径；Turing 上 AWQ kernel 支持有限 | 不推荐 |
| **INT8 动态量化（torch）** | 可 | 🔴 CPU 上才明显，GPU 小模型无正收益 | 低 | 不推荐 |
| **bitsandbytes 8bit/4bit** | 可 | 🔴 为省显存设计，非提速；小模型反而慢 | 依赖新增 | 不推荐 |
| **KV cache 量化** | vLLM/部分框架支持 | 🟡 省显存为主，本就不缺；延迟无感 | 需框架支持 | 无必要 |
| **flow/hift 量化** | 理论可 | 🔴 这两段非 TTFB 主瓶颈（见 TRT 分析），且非标准架构量化工具链缺失 | 高 | 不推荐 |

### 5.4 结论
- **量化不是本服务 TTS 延迟的解法**。它与 vLLM 是「显存/吞吐」工具，而我们既不缺显存、又是单卡低并发 + 极小模型 + batch=1 的延迟敏感场景——三个前提全不满足。
- fp16 已实测负面（同机理），量化（更激进的低精度 + dequant 开销）预期**同样负面或更差**，不值得投入验证成本。
- **真正能压 TTS 延迟的仍是**：① 零成本的首句从短切 + ASR 聚合窗调优（§4.2-1）；② 若要根治 LLM 串行瓶颈，需独立 GPU（涉及采购，AI 不代购）或更换更快的 TTS 架构。

---

## 附：数据采集方法（可复现）
- GPU/进程显存：`nvidia-smi --query-gpu` / `--query-compute-apps`（node1 容器内）。
- 权重大小：容器内 `du -sh /models/*`。
- 峰值：3 路并发 `/api/v1/tts` 期间轮询 `memory.used`。
- 软件栈：容器内 `torch.__version__` / `torch.cuda.get_device_capability()`（返回 (7,5)=Turing）。
- LLM 架构/规模：容器内 CosyVoice2 `config.json`（Qwen2, hidden 896 / 24 层 / bf16）+ `cosyvoice/llm/llm.py`（`Qwen2ForCausalLM`）。
- 全程只读，未改动任何业务代码或机器配置。
