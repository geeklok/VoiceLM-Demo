> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# TTS 文本处理链路 — 架构说明与排查手册

> 创建：2026-07-07 | 定位：从「一段文本进来」到「PCM 音频出去」，逐层拆解经过哪些处理、每层做什么/不做什么、出问题时怎么定位。面向**排查**，与顶层《[语音合成TTS-CosyVoice2方案](cosyvoice2.md)》互补。
>
> 核心结论（先看这条）：**服务层固定有 `strip_markdown`，可选挂载 `apply_domain_tn` 领域薄层，并加了一个极窄的纯数字中文兜底；主 TN（`text_normalize`）仍在 CosyVoice2 模型内部，默认开启，我们不关闭也不替换。**

---

## 1. 全链路总览（文字版）

一段文本从请求到音频，依次经过 **5 个处理阶段**，前 3 个在**我们的服务层**，后 2 个在 **CosyVoice2 模型内部**：

```
用户文本
  │
  ▼  ┌─────────────── 服务层（app/ 代码，我们可控）────────────────┐
  ① strip_markdown(text)          去 Markdown 语法 → 适合朗读的纯文本
  │                                （不是 TN；无开关，无条件生效）
  ▼
  ② apply_domain_tn(text)         领域 TN 薄层：仅按前端勾选类别做预转换
  │    + apply_default_tts_tn      默认纯数字中文兜底：1234→一千二百三十四
  ▼
  ③ split_sentences(text)         按标点分句，让首句短 → 降 TTFB
  │                                （仅流式路径；开关 tts_sentence_stream）
  ▼  └───────────────────────────────────────────────────────────┘
     inference_zero_shot(text, ..., stream=?, speed=?)   ← 进入模型
  │
  ▼  ┌─────────────── 模型层（CosyVoice2 内部，我们不改）──────────┐
  ④ text_normalize(text)          内置 TN：数字读法、标点规整、中英混排
  │                                （text_frontend=True 默认开；我们未传该参数）
  ▼
  ⑤ LLM 自回归生成                 Qwen2-0.5B → 逐 token 生成语音表征 → 声码器
  │
  ▼  └───────────────────────────────────────────────────────────┘
PCM 音频（24kHz 单声道 float32）→ 编码为 WAV / 裸 int16 下发
```

> ⚠️ **服务层薄层和模型层 ④ 是两回事，最容易混淆**：`strip_markdown` 只删 md 符号；`apply_domain_tn` 只处理用户显式勾选的少数领域规则；`apply_default_tts_tn` 只兜底整段纯数字输入。完整数字/标点/中英混排仍交给模型内置 `text_normalize`。

---

## 2. 逐层详解

### 阶段 ① `strip_markdown`（服务层，无条件生效）

| 项 | 内容 |
|---|---|
| 代码 | [text_clean.py](../../backend/app/utils/text_clean.py) |
| 调用点 | 一次性：[dispatcher.py:162](../../backend/app/orchestration/dispatcher.py#L162)（`synthesize` 前）；流式：[dispatcher.py:204](../../backend/app/orchestration/dispatcher.py#L204)（`split_sentences` **之前**）|
| 做什么 | 链接 `[文字](url)`→留「文字」丢 URL；图片 `![]()` 整体删；裸 URL / `www.` 删；去 `** __ * _ ~~` 与行内代码符号；代码围栏去围栏留内部文本；行首 `#`/`>`/`-*+`/`1.` 标记去掉转停顿；表格分隔行删、单元格竖线 `|`→中文逗号；折叠多余空白 |
| **不做** | **不做 TN**：不改数字读法、不规整标点、不做中英混排归一化。这些交给服务层薄 TN 或模型层 ④ |
| 开关 | 无。纯文本经过它基本不变（只折叠空白），对非 md 输入无副作用，故不设 env flag |

### 阶段 ② `apply_domain_tn` / `apply_default_tts_tn`（服务层薄 TN）

| 项 | 内容 |
|---|---|
| 代码 | [domain_tn.py](../../backend/app/utils/domain_tn.py) |
| 调用点 | `strip_markdown` 之后、`split_sentences` / 模型内置 TN 之前；一次性与流式路径一致 |
| `apply_domain_tn` | 只按前端勾选类别生效；未勾选或空列表时不做领域规则 |
| `apply_default_tts_tn` | 无开关、极窄兜底：仅当整段文本就是数字/小数时，把 `1234` 转成「一千二百三十四」，避免模型因缺少中文上下文读成英文 `Twelve Thirty-four` |
| 不做什么 | 不处理句子里的普通数字；验证码/手机号/订单号逐位读仍由用户勾选 `digit_string` 表达 |


### 阶段 ③ `split_sentences`（服务层，仅流式，可开关）

| 项 | 内容 |
|---|---|
| 代码 | [text_split.py:19-47](../../backend/app/utils/text_split.py#L19-L47) |
| 调用点 | 仅 `tts_stream` 的 `guarded()` 内（[dispatcher.py:205-212](../../backend/app/orchestration/dispatcher.py#L205-L212)）；`tts_file` **不分句** |
| 做什么 | 主标点（`。！？!?；;\n`）切句；超长句按次标点（`，,、 `）/硬截断切到 `max_chars` 内；`min_chars>0` 时贪心合并过短段（消除句间播放间隙）|
| 为什么 | CosyVoice2 TTFB 随输入长度增长；让**首个短句**先出首块 → TTFB 绑定到短输入 |
| 开关 | `tts_sentence_stream`（默认 False）；`tts_max_sentence_chars`（默认 60）；`tts_min_sentence_chars`（默认 0）。见 [config.py:112-120](../../backend/app/config.py#L112-L120) |
| **关键约束** | 整个分句循环必须在**同一个 `tts_slot()` 内**，否则逐句重排队触发 429；`ttfb_ms` 只在**全局首个 chunk** 翻转 |

### 阶段 ④ `text_normalize` / 内置 TN（模型层，默认开）

| 项 | 内容 |
|---|---|
| 触发 | `inference_zero_shot(text, "", "", zero_shot_spk_id=voice, stream=stream, speed=speed)`（[cosyvoice_engine.py:100-103](../../backend/app/engines/cosyvoice_engine.py#L100-L103)）——**未传 `text_frontend`**，用 CosyVoice 默认值 |
| 做什么 | CosyVoice 前端 `text_normalize`：数字→读法、标点规整、中英分词等文本归一化（TN） |
| **不做** | 不剥离任何 markdown 语法（所以才需要服务层 ①）|
| 状态 | `text_frontend` 默认 `True` → TN 开启。我们既没显式开也没显式关，走默认 |
| ✅ 已实锤 | 2026-07-07 node1 容器内 `inspect.signature` 确认：签名为 `(self, tts_text, prompt_text, prompt_wav, zero_shot_spk_id='', stream=False, speed=1.0, text_frontend=True)`，**`text_frontend` 默认 `True`**，内置 TN 确认开启 |

### 阶段 ⑤ LLM 自回归生成（模型层）

| 项 | 内容 |
|---|---|
| 引擎 | CosyVoice2-0.5B，底层 LLM 为 Qwen2-0.5B |
| 做什么 | 归一化后的文本 → 逐 token 自回归生成语音表征 → 声码器 → PCM |
| 关键 | TTFB 瓶颈在此段（串行 token 生成）；这也是 fp16/vLLM/TRT 优化的目标层，但均评估不采纳 |
| 输出 | 24000Hz 单声道 float32 PCM |

---

## 3. 两种调用路径的链路差异

| 阶段 | 一次性 `POST /api/v1/tts` | 流式 `WS /ws/tts` | 语音聊天 `WS /ws/chat` |
|---|---|---|---|
| 入口 | [routes_tts.py:23-48](../../backend/app/api/routes_tts.py#L23-L48) | [routes_tts.py:51-102](../../backend/app/api/routes_tts.py#L51-L102) | conversation.py `_respond`→`speak` |
| 编排方法 | `dispatcher.tts_file` | `dispatcher.tts_stream` | 每句调 `dispatcher.tts_stream` |
| ① strip_markdown | ✅ | ✅ | ✅（每句都过）|
| ② 薄 TN | ✅ 领域 TN + 纯数字兜底 | ✅ 领域 TN + 纯数字兜底 | ✅（每句都过）|
| ③ split_sentences | ❌ 不分句 | ✅（若开关开）| ⚠️ **两层分句**：conversation 先聚句下发单句，dispatcher 内再 `split_sentences` |
| 引擎方法 | `synthesize`（stream=False）| `synthesize_stream`（stream=True）| `synthesize_stream` |
| ④ 内置 TN | ✅ 默认开 | ✅ 默认开 | ✅ 默认开 |
| speed 变速 | ✅ **生效** | ❌ **失效**（写死 1.0）| ❌ 失效 |

> **聊天的双层分句**要特别注意：LLM 逐 token 输出 → conversation 层用 `_pop_complete` 聚成完整句 → 每句作为一次 `tts_stream` 调用 → dispatcher 内若 `tts_sentence_stream=True` 会**再**对这一句 `split_sentences`（通常就是它自己）。排查聊天分句异常时，要分清是 conversation 层聚句还是 dispatcher 层分句。

---

## 4. 关键约束与已知坑

1. **流式 speed 失效（固有限制）**：`synthesize_stream` 内 speed 硬编码 `1.0`（[cosyvoice_engine.py:130-131](../../backend/app/engines/cosyvoice_engine.py#L130-L131)），CosyVoice2 流式不做插值变速。→ 一次性合成变速有效，流式/聊天无效。前端已据此：独立 TTS 页标注「仅一次性合成生效」，聊天页移除语速控件。
2. **服务层只做薄 TN 是设计选择**：我们不在服务层做「全量独立 TN」替换模型，只补模型明确不做/真实出错的窄规则（markdown、用户勾选的领域 TN、纯数字中文兜底），避免与模型归一化重复/冲突。
3. **单卡 GPU 并发闸**：TTS 走 `tts_slot()`（[limiter.py:70-71](../../backend/app/orchestration/limiter.py#L70-L71)），默认 `tts_concurrency=2`；排不到 → 429/`busy`。分句循环必须在**同一个 slot** 内。
4. **同步生成器→异步桥接**：CosyVoice 推理是同步生成器，用线程 + `asyncio.Queue` 桥接（[cosyvoice_engine.py:119-146](../../backend/app/engines/cosyvoice_engine.py#L119-L146)），异常经队列透传，`_DONE` 哨兵收尾。
5. **音色**：合成传 `zero_shot_spk_id`，音色在**加载时**用 `add_zero_shot_spk` 从「参考音频+文本」预注册（[cosyvoice_engine.py:67-80](../../backend/app/engines/cosyvoice_engine.py#L67-L80)）。传入未注册音色 → 回退默认音色（[cosyvoice_engine.py:90-98](../../backend/app/engines/cosyvoice_engine.py#L90-L98)）。

---

## 5. TN 方案选型：外部独立 TN vs 模型内置 TN

> 承 §4 约束 #2「服务层只做薄 TN 是设计选择」。此处展开两种 TN 方案的取舍与建议。

### 5.1 结论

**维持现状：以模型内置 TN 为主 + 一层「薄」外部预处理只补模型不做或真实出错的部分（`strip_markdown`、可选 `apply_domain_tn`、纯数字兜底）。不建议在 TTS 前挂一整套独立 TN 去替换模型 TN——除非有明确证据表明模型 TN 在某类文本上系统性读错。**

理由：独立 TN 的收益是「可控/可定制」，但代价是要么与模型 TN 重复冲突，要么关掉模型 TN 后须对全部归一化负全责（中文 TN 本身极难做好），还会引入「训练/推理文本不一致」导致音质下降的风险。本项目迄今唯一暴露的真实问题是 markdown 符号被念——那不是 TN 能解决的，已用薄预处理层解决；未出现「数字/单位/多音字系统性读错」这类真正需要独立 TN 的证据。

### 5.2 两种方案主要区别

| 维度 | 独立 TN（挂 TTS 前） | 模型内置 TN（现状） |
|---|---|---|
| 代码/依赖 | 需自建 TN 模块（中文 TN 是重活：数字/日期/货币/单位/多音字/中英混排） | 零代码零依赖，`text_frontend=True` 默认跑 |
| 可控性 | 🟢 强：定制领域词、指定读法、修多音字、注入停顿 | 🔴 黑盒，读错难改 |
| 可观测/可测 | 🟢 归一化后文本可打印、可单测 | 🔴 看不到实际进声学模型的文本（须进容器灌 `text_normalize`） |
| 训练/推理一致性 | 🔴 风险：TN 输出可能偏离模型训练时见过的归一化文本 → 反而降质 | 🟢 与模型 tokenizer/训练数据同源，一致性最好 |
| 双重归一化风险 | 🔴 若模型 TN 不关 → 同处理做两遍，可能互相打架 | — |
| 关掉模型 TN 的代价 | 🔴 `text_frontend=False` 关掉的是**整个 frontend**（不止 TN，含分词等），任何未覆盖输入裸进 tokenizer（如当初 markdown 被逐字念） | — |
| 模型升级 | 🟢 TN 与模型解耦 | 🔴 升级 CosyVoice 可能改变 TN 行为 |
| 延迟 | 规则式 TN 通常可忽略 | 已含在推理内 |

### 5.3 关键技术陷阱（若真要上独立 TN）

不是「加一层」这么简单，有个二选一的坑：

- **模型 TN 不关** → 你的 TN + 模型 TN 各做一遍，轻则冗余，重则冲突（如把「3.5」转成「三点五」，模型 TN 再处理一次可能出怪结果）。
- **模型 TN 关掉（`text_frontend=False`）** → 关的是整个 frontend，不只 TN。独立 TN 必须**完整覆盖**模型原本做的全部归一化 + 文本切分，任何缺口都会像 markdown 问题一样裸进模型。这是从「补充」变成「全责替换」，工作量与风险都跳一个量级。

即：独立 TN 实际上「要么和内置 TN 打架，要么替换掉内置 TN 全责兜底」，没有轻松的中间态。

### 5.4 推荐架构（即现状哲学）

保持**分层薄预处理**，而非「独立 TN 替换」：

```
用户文本
  → strip_markdown()       [薄层: 只做模型 TN 不做的——剥 md 符号/URL]
  → apply_domain_tn()      [薄层: 只做用户勾选的领域规则]
  → apply_default_tts_tn() [薄层: 只兜底整段纯数字中文读法]
  → (流式) split_sentences  [薄层: 分句降 TTFB]
  → 模型内置 TN             [text_frontend=True, 归一化主力]
  → LLM 生成
```

薄预处理层的判据：**只加模型 TN 明确不做、且已有真实问题的规则**（markdown 即典型）。它无开关、对纯文本近乎无副作用（见 §2 阶段① `strip_markdown`）。

### 5.5 何时才值得上独立/领域 TN（触发条件）

不一刀切否死；满足以下**任一**再评估：

- ✅ 出现**系统性**读错：某类领域术语/产品名/特定数字单位格式反复念错，且模型 TN 无法通过输入微调绕过。
- ✅ 有**强定制**需求：必须按业务规定读法（金额、编号、多音字人名等）。
- ✅ 即便如此，也**优先做「领域 TN 薄层」**（只处理这几类，放模型 TN 之前做预转换，让模型 TN 拿到已规范文本），而非关掉模型 TN 全责替换。

### 5.6 领域 TN 常见场景清单

**触发信号**（通用 TN 会在这三种情况失灵，也是判断要不要上领域 TN 的判据）：

1. **读法有歧义，须靠领域上下文才能定** —— 如 `3-5`：数量语境「三到五」、算式「三减五」、比分「三比五」、日期「三月五日」，通用 TN 无从判断。
2. **依赖专有词表/知识** —— 药品名、人名地名、品牌、型号、法条；无词典就拆错音或读错多音字。
3. **有强制读法规范** —— 金额、编号、验证码，业务上「必须逐位读/按规范读」，不能由模型自由发挥。

**常见领域场景**：

| 领域 | 典型文本 | 通用 TN 为何不够 |
|---|---|---|
| 金融 | ¥1,234.56、年化 3.5%、股票代码、汇率 | 大额分级（万/亿）、货币单位、「点」vs「元角分」 |
| 电信/号码 | 手机号、身份证、银行卡、**验证码**、订单号、快递单号 | 须**逐位读**，通用 TN 常整体当数量读 |
| 医疗 | 药名、剂量 `0.5g bid`、化验指标 WBC/ALT、缩写 COPD | 专有词表 + 剂量单位 + 缩写读法 |
| 日期时间 | `2024/3/5`、`3-5`、`周三`、`Q3` | 格式歧义（3-5 是范围还是日期） |
| 计量单位 | km/h、m²、℃、‰、°、mAh | 符号→读法、单位组合 |
| 数学/科技 | `x²`、`H₂O`、版本号 `v2.3.1`、型号、API 名 | 上下标/运算符；版本号逐段读非小数 |
| 地址/导航 | 门牌、路名、邮编、经纬度 | 多音字地名（重庆/乐山）、逐位 vs 整读 |
| 法律/政务 | 《民法典》第1234条、文号、案号 | 编号规范读法 + 专名 |
| 人名/专名 | 多音字人名、品牌、外文名 | 多音字消歧（仇/单/华作姓氏） |
| 符号/结构 | 比分 3:2、区间、范围号、URL/邮箱 | 冒号/连字符/@ 的语境读法 |
| 缩写/中英混排 | NASA（整读）vs FBI（逐字母）、「C 位」、「3A」 | 首字母词整读/逐读判断 |

**核心共性——数字的「语义类型」**：上表约一半场景本质是同一问题：同样是数字，是「数量」还是「编号」决定读法完全不同。

- 数量：`1580` → 「一千五百八十」
- 编号（电话/验证码/卡号）：`1580` → 「一五八零」

通用 TN 默认按数量读，所以**「标识符类」数字（验证码、单号、卡号、房间号、航班号）最易翻车**——这也是实践中领域 TN 最高频、最该优先做的一类。

**结合本项目（CosyVoice + Qwen 闲聊助手）**：

- **大概率用不上** —— 日常口语对话极少出现验证码/药名剂量/法条，模型内置 TN 足够。
- **真要出问题，最可能是**：① 用户让它读**验证码/手机号/订单号**（逐位读需求）；② 回答里带**版本号/型号/URL**。此时按 §5.5 做**只覆盖这几类的领域 TN 薄层**（模型 TN 之前预转换），而非关掉模型 TN 全责替换。
- **优先级判据**：先看有没有真实读错的样本，再决定做哪类；不要预防性地把整张表都实现，否则又落回「独立 TN 全责兜底」的重坑。

### 5.7 领域 TN 薄层落地现状与实验类转正路线图

> 更新：2026-07-13。§5.5/§5.6 是「要不要做、做哪类」的判据；本节记录**已经做到哪一步**，以及剩余实验类怎么转正。对应代码：[domain_tn.py](../../backend/app/utils/domain_tn.py)。

#### 5.7.1 现状快照

已按 §5.4「薄预处理层」哲学落地一个**可交互的领域 TN 薄层**：挂在 `strip_markdown` 之后、`split_sentences`/模型内置 TN 之前，前端 TTS 页多选勾选、按类做预转换（[dispatcher.py:165](../../backend/app/orchestration/dispatcher.py#L165) 与 [dispatcher.py:209](../../backend/app/orchestration/dispatcher.py#L209)）。11 类中 **7 类已真实现**、4 类仍为 noop 占位（UI 置灰不可勾选，并标「实验性」）：

| 类别 id | 中文名 | 状态 | 规则要点 |
|---|---|---|---|
| `digit_string` | 号码逐位读 | ✅ 已实现 | 显式开关：勾选后 ≥2 位数字串一律逐位读（`_DIGIT_READ` 幺五八零，空格分隔）+ 带分隔电话号；孤立单个数字不拆 |
| `units` | 计量单位 | ✅ 已实现 | 符号→读法，先长后短匹配（km/h 先于 km）|
| `version` | 版本号 | ✅ 已实现 | ≥2 个点 → 逐段「点」，v→「版本」|
| `symbols` | 符号(比分/区间) | ✅ 已实现 | 3:2→3比2；3~5 / 3-5→3到5 |
| `datetime` | 日期时间 | ✅ 已实现（2026-07-13 转正）| 4 位年打头的 `/`-`-` 日期→年月日；`hh:mm[:ss]`→时分秒；月/日限合法范围避免误伤年份区间 |
| `finance` | 金融金额 | ✅ 已实现（2026-07-13 转正）| 去千分位逗号、货币符号→词、人民币小数→元角分；外币小数保留 |
| `abbrev` | 缩写/中英混排 | ✅ 已实现（2026-07-13 转正）| 大写字母串默认逐字母拼读，小白名单（NASA/NATO…）整读；3A/5G 单字母不拆 |
| `medical` | 医疗药名剂量 | 🧪 实验性(noop) | 需药品词表 + 剂量单位 + 缩写读法 |
| `address` | 地址导航 | 🧪 实验性(noop) | 需多音字地名词典 |
| `legal` | 法律政务 | 🧪 实验性(noop) | 需法条/案号编号规范 + 专名 |
| `name` | 人名专名 | 🧪 实验性(noop) | 需姓氏多音字消歧（仇/单/华）|

> ⚠️ **执行顺序耦合**（`_ORDER`，[domain_tn.py:233](../../backend/app/utils/domain_tn.py#L233)）：`datetime → finance → symbols → units → version → abbrev → digit_string`。两条硬约束：① `datetime` 必须在 `symbols` **之前**，否则 `2024-3-5` 会被区间规则先吃成「2024到3」；② `digit_string`（逐位读）必须**最后**，否则日期/金额/版本号/单位里的数字会被误当长数字串逐位拆。新增类别务必按此语义插位。

#### 5.7.2 实验类转正的三档难度

剩余 4 个实验类（及未来新类）按「规则能不能做对」分三档，难度递增：

| 档位 | 类别 | 能否纯规则 | 说明 |
|---|---|---|---|
| **档一 · 纯规则** | ~~datetime / abbrev~~（已转正）、finance 金额 | 🟢 能 | 格式强信号、读法确定，正则即可。**本轮已把 datetime/finance/abbrev 全部转正**，档一基本清空 |
| **档二 · 需词典** | name / address / legal（及 finance 的证券简称部分）| 🟡 半 | 骨架可规则化，但正确读音依赖外部词表（多音字姓氏、地名、法条编号）。须引入并维护词典，有覆盖率/更新成本 |
| **档三 · 最难** | medical | 🔴 难 | 药名/化验缩写既要专业术语库又要消歧（同名不同读、剂量单位组合），错读风险最高，投入产出比最低，最后做或不做 |

> 判据回顾（§5.5）：**先有真实读错样本再做**。档二/档三不要预防性实现——闲聊场景极少触发，贸然接词典反而把薄层拖成「全责兜底」重坑。

#### 5.7.3 转正标准四步流程

把一个实验类从 noop 变成真实现，固定四步（以本轮 datetime/finance/abbrev 为样板）：

1. **写规则**：在 [domain_tn.py](../../backend/app/utils/domain_tn.py) 加 `_tn_<类别>(text) -> text` 纯函数，幂等、对不含目标模式的文本无副作用；歧义拿不准就**不改**（兜底优先于激进）。
2. **注册 + 插位**：函数加进 `_IMPL_FUNCS`，并按语义把 id 插入 `_ORDER` 正确位置（尤其相对 `digit_string`/`symbols` 的先后）。
3. **改状态**：`DOMAIN_TN_CATEGORIES[<id>]` 由 `(False, …)` 改 `(True, …)`；`IMPLEMENTED` 自动跟随。
4. **补测 + 验证**：在 [test_domain_tn.py](../../backend/tests/test_domain_tn.py) 加正例（生效）+ 反例（不误伤）+ 组合顺序用例；`ASR_ENGINE=stub TTS_ENGINE=stub pytest` 全绿。

#### 5.7.4 架构改进：动态类别接口（已消除前后端双维护）

**背景问题**：曾经前端 `TtsPage.tsx` 手写一份 `DOMAIN_TN_OPTIONS`（含 `impl` 标记）、后端 `domain_tn.py` 一份 `DOMAIN_TN_CATEGORIES`，转正时须同步改两处，极易漏改导致「后端已实现但前端仍标实验性」之类的不一致。

**已落地方案**（2026-07-13）：后端新增 `GET /api/v1/tn-categories`（[routes_tts.py:24-35](../../backend/app/api/routes_tts.py#L24-L35)），直接由 `DOMAIN_TN_CATEGORIES` 派生返回 `[{id,label,impl}]`；前端 [client.ts](../../frontend/src/api/client.ts) 加 `fetchTnCategories`，[TtsPage.tsx](../../frontend/src/pages/TtsPage.tsx) 启动时拉取动态渲染，**删除了手写常量**。

**收益**：`domain_tn.py` 成为类别清单的**单一事实来源**，此后转正只走 §5.7.3 四步（纯后端），前端自动跟随，无需再动前端类别列表。`test_api.py::test_tn_categories` 锁定接口与注册表一致。

**UI 规则**：`impl=True` 的类别可勾选并随请求传 `domain_tn`；`impl=False` 的实验类当前不可用，前端置灰并禁用 checkbox，避免用户误以为勾选后会生效。

---

## 6. 排查手册（现象 → 定位）

| 现象 | 最可能的层 | 定位方法 |
|---|---|---|
| **念出 `#`/`*`/URL 等符号** | 阶段① `strip_markdown` 失效或未覆盖该语法 | 容器内 `python -c "from app.utils.text_clean import strip_markdown; print(strip_markdown('你的输入'))"` 看输出是否还残留符号；若残留 → 补 `text_clean.py` 规则 |
| **数字读法/标点怪（如 "1.5" 没读成 "一点五"）** | 阶段③ 内置 TN | 这是模型 TN 行为，非服务层；确认 `text_frontend` 未被关；必要时进容器对 `frontend.text_normalize` 单独灌文本实锤 |
| **变速滑块无效** | 阶段④ 路径 | 确认是否走流式（流式 speed 写死 1.0）；一次性合成才生效 |
| **长文本首包慢** | 阶段③ + ⑤ | 确认 `tts_sentence_stream` 是否开；看 QoS `ttfb_ms`；分句开启后首句应显著更快 |
| **聊天分句异常（断句怪/句间卡顿）** | conversation 聚句 或 阶段③ | 分清两层：conversation `_pop_complete` 聚句 vs dispatcher `split_sentences`；调 `tts_min_sentence_chars` 消除句间隙 |
| **429 / busy** | GPU 并发闸 | `tts_concurrency` 已满且排队超时；看是否有分句循环误在 slot 外重排队 |
| **音色不对/回退默认** | 音色注册 | 看启动日志 `add_zero_shot_spk` 是否成功、参考音频路径是否存在 |
| **合成报错中断** | 阶段④ 桥接 | 异常经队列透传到 WS `error` 帧；看后端 `tts ws error` 日志 |

### 实锤命令速查（容器内）

```bash
# ① strip_markdown 效果
docker exec deploy-backend-1 python -c "from app.utils.text_clean import strip_markdown; print(repr(strip_markdown('## 标题\n**粗** 见 [文档](http://x.com)')))"

# ② apply_default_tts_tn 纯数字兜底
docker exec deploy-backend-1 python -c "from app.utils.domain_tn import apply_default_tts_tn; print(apply_default_tts_tn('1234'))"

# ③ split_sentences 效果
docker exec deploy-backend-1 python -c "from app.utils.text_split import split_sentences; print(split_sentences('你好。今天天气不错，我们出去走走吧！', 60, 0))"

# ④ 内置 TN 默认值实锤（确认 text_frontend 默认 True）
docker exec deploy-backend-1 python -c "import inspect; from cosyvoice.cli.cosyvoice import CosyVoice2; print(inspect.signature(CosyVoice2.inference_zero_shot))"

# 生效配置
docker exec deploy-backend-1 python -c "from app.config import get_settings as g; s=g(); print('sentence_stream=',s.tts_sentence_stream,'max=',s.tts_max_sentence_chars,'min=',s.tts_min_sentence_chars,'tts_conc=',s.tts_concurrency)"
```

---

## 7. 核实记录

- **`text_frontend` 默认值** ✅ **已确认**（2026-07-07，node1 容器内实锤）：
  ```
  CosyVoice2.inference_zero_shot 签名：
  (self, tts_text, prompt_text, prompt_wav, zero_shot_spk_id='', stream=False, speed=1.0, text_frontend=True)
  → text_frontend 默认 True
  ```
  结论：我们调用时未传 `text_frontend`，走默认 `True`，**CosyVoice2 内置 TN（`text_normalize`）确认开启**。§1 全链路图与 §2 阶段③ 的判断成立。

---

## 8. 相关文档

- 顶层方案：《[语音合成TTS-CosyVoice2方案](cosyvoice2.md)》
- 分句流式 TTFB 优化 + markdown 剥离 + fp16 负面结论：《[分句流式TTS-TTFB优化方案](sentence-streaming.md)》
- TTS 作为聊天回复底层：《[语音聊天-语音到语音对话方案](../chat/speech-to-speech.md)》
