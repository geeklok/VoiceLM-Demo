from __future__ import annotations

import re

# 领域 TN (Text Normalization) 预处理层。
#
# 定位: 挂在 strip_markdown 之后、split_sentences / 模型内置 TN 之前的「薄层」。
# 只做 CosyVoice 内置 TN 明确不擅长、且规则可做对的归一化; 需要词典/领域知识的
# 类别 (医疗药名/多音字人名地名/品牌/法条等) 无法用规则正确实现, 定义为 noop 占位,
# 对外标注「实验性·暂未生效」, 避免误导 (见《TTS文本处理链路-架构说明与排查手册》§5)。
#
# 设计原则:
#   - 每类是一个 text->text 的纯函数, 引擎无关, 幂等安全 (对不含目标模式的文本无副作用)。
#   - 顺序: 先做「结构类」(symbols/units/version) 再做 digit_string, 避免逐位读把
#     版本号/单位里的数字也拆掉。
#   - noop 类保持原样返回, 只为让前端选项与后端类别对齐, 后续接词典再实现。

# ---- 类别注册表 ------------------------------------------------------------
# key: 类别 id (与前端复选框 value 对齐); value: (是否已实现, 中文名)
DOMAIN_TN_CATEGORIES: dict[str, tuple[bool, str]] = {
    # 规则可做对 -> 已实现
    "digit_string": (True, "号码逐位读"),
    "units": (True, "计量单位"),
    "version": (True, "版本号"),
    "symbols": (True, "符号(比分/区间)"),
    "datetime": (True, "日期时间"),
    "finance": (True, "金融金额"),
    "abbrev": (True, "缩写/中英混排"),
    # 需词典/领域知识 -> noop 占位 (实验性·暂未生效)
    "medical": (False, "医疗药名剂量"),
    "address": (False, "地址导航"),
    "legal": (False, "法律政务"),
    "name": (False, "人名专名"),
}

IMPLEMENTED = tuple(k for k, (ok, _) in DOMAIN_TN_CATEGORIES.items() if ok)


# ---- 默认中文兜底: 纯数字输入避免被模型内置 TN 误判为英文 ----------------------
# CosyVoice 内置 text_normalize 在只有 "1234" 这类全 ASCII 数字、没有任何中文上下文时，
# 可能按英文年份/时间读成 "Twelve Thirty-four"。这不是「领域 TN」语义，而是中文
# TTS 页面的一层极窄兜底: 仅当整段文本本身就是一个数字/小数时，把它转成中文数量读法；
# 含中文上下文的句子、日期/时间/版本号/号码等仍交给模型内置 TN 或用户勾选的领域 TN。
_BARE_NUMBER = re.compile(r"^\s*([+-]?)((?:\d+|\d{1,3}(?:,\d{3})+))(?:\.(\d+))?\s*$")
_CN_DIGITS = "零一二三四五六七八九"
_CN_SMALL_UNITS = ["", "十", "百", "千"]
_CN_BIG_UNITS = ["", "万", "亿", "兆"]


def _read_digits_zh(s: str) -> str:
    """逐位读数字（用于带前导 0 的纯数字，避免 0012 被读成十二）。"""
    return "".join(_CN_DIGITS[int(ch)] for ch in s)


def _section_to_zh(n: int, *, drop_leading_one: bool = True) -> str:
    """0 < n < 10000 的中文数量读法。"""
    s = str(n)
    out: list[str] = []
    zero_pending = False
    for i, ch in enumerate(s):
        d = int(ch)
        unit_pos = len(s) - i - 1
        if d == 0:
            zero_pending = bool(out)
            continue
        if zero_pending:
            out.append("零")
            zero_pending = False
        out.append(_CN_DIGITS[d] + _CN_SMALL_UNITS[unit_pos])
    text = "".join(out)
    # 十、十一、十二……自然读法不带开头的「一」。
    return text[1:] if drop_leading_one and text.startswith("一十") else text


def _int_to_zh(n: int) -> str:
    if n == 0:
        return "零"
    groups: list[int] = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    out: list[str] = []
    zero_pending = False
    for idx in range(len(groups) - 1, -1, -1):
        group = groups[idx]
        if group == 0:
            zero_pending = bool(out)
            continue
        if out and (zero_pending or group < 1000):
            out.append("零")
        out.append(
            _section_to_zh(group, drop_leading_one=(not out)) + _CN_BIG_UNITS[idx]
        )
        zero_pending = False
    return "".join(out)


def apply_default_tts_tn(text: str) -> str:
    """中文 TTS 默认兜底归一化。

    当前只处理「整段就是数字」的极窄场景，避免无中文上下文时被模型内置 TN 当英文读。
    领域语义仍由 apply_domain_tn 的显式类别负责；非纯数字文本原样返回。
    """
    if not text:
        return text or ""
    m = _BARE_NUMBER.match(text)
    if not m:
        return text

    sign, int_part, dec_part = m.groups()
    raw_int = int_part.replace(",", "")
    if len(raw_int) > 1 and raw_int.startswith("0"):
        zh = _read_digits_zh(raw_int)
    else:
        zh = _int_to_zh(int(raw_int))
    if dec_part is not None:
        zh += "点" + _read_digits_zh(dec_part)
    if sign == "-":
        zh = "负" + zh
    return zh


# ---- digit_string: 标识符类数字逐位读 --------------------------------------
# 「数量 vs 编号」是领域 TN 最高频的坑: 通用 TN 默认按数量读 (1580->一千五百八十),
# 但验证码/手机号/卡号/订单号须逐位读 (1580->幺五八零)。digit_string 是用户显式勾选的
# 开关 (label「号码逐位读」), 勾选即表达「把这里的数字当号码逐位读」, 因此对 >=2 位的
# 数字串一律逐位拆 (用空格分隔, 让模型逐位读); 孤立单个数字不拆 (既非「串」, 逐位==数量)。
# 注: 阈值曾为 >=6 (当它还是「自动判定长串=编号」时的防误伤设计), 导致勾选后 4 位数
# (如 1234) 不生效、被模型当年份读成英文 "Twelve Thirty-four" —— 与开关语义矛盾, 已下调。
# 小数由前后 (?<![\d.]) / (?![\d.]) lookaround 保护, 不受影响。
_LONG_DIGITS = re.compile(r"(?<![\d.])\d{2,}(?![\d.])")
# 电话号码常见带分隔符形态: 138-1234-5678 / 010 1234 5678
_PHONE_SEP = re.compile(r"(?<!\d)\d{3,4}[-\s]\d{3,4}[-\s]\d{3,5}(?!\d)")
_DIGIT_READ = {
    "0": "零", "1": "幺", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}


def _read_digits(s: str) -> str:
    # 只逐位读数字, 保留其中的分隔符位置为轻停顿 (转空格)。
    return " ".join(_DIGIT_READ.get(ch, ch) for ch in s if ch.isdigit())


def _tn_digit_string(text: str) -> str:
    text = _PHONE_SEP.sub(lambda m: _read_digits(m.group(0)), text)
    text = _LONG_DIGITS.sub(lambda m: _read_digits(m.group(0)), text)
    return text


# ---- units: 计量单位符号 -> 读法 -------------------------------------------
# 只映射符号/缩写单位, 不动数字本身 (数量读法交给模型内置 TN)。
_UNIT_MAP = [
    ("km/h", "千米每小时"), ("m/s", "米每秒"), ("kg", "千克"), ("mg", "毫克"),
    ("mL", "毫升"), ("ml", "毫升"), ("m²", "平方米"), ("m³", "立方米"),
    ("km²", "平方千米"), ("cm", "厘米"), ("mm", "毫米"), ("Hz", "赫兹"),
    ("kWh", "千瓦时"), ("mAh", "毫安时"), ("℃", "摄氏度"), ("℉", "华氏度"),
    ("‰", "千分之"), ("%", "百分之"), ("°", "度"),
]
# 先长后短, 避免 km 先于 km/h 命中。
_UNIT_MAP.sort(key=lambda kv: len(kv[0]), reverse=True)


def _tn_units(text: str) -> str:
    for sym, read in _UNIT_MAP:
        text = text.replace(sym, read)
    return text


# ---- version: 版本号逐段读 (v2.3.1 -> v 二点三点一 的「点」分段, 非小数) ------
# 形如 2.3.1 / v1.0.0 (>=2 个点) 判为版本号: 每段数字保留, 点读「点」, 不当小数聚合。
_VERSION = re.compile(r"(?<![\d.])[vV]?\d+(?:\.\d+){2,}(?![\d.])")


def _tn_version(text: str) -> str:
    def repl(m: re.Match) -> str:
        s = m.group(0)
        prefix = ""
        if s[0] in "vV":
            prefix = "版本 "
            s = s[1:]
        return prefix + " 点 ".join(s.split("."))
    return _VERSION.sub(repl, text)


# ---- symbols: 结构符号语境读法 (比分/区间) ----------------------------------
# 3:2 -> 三比二 (比分); 3~5 / 3-5 (数字间) -> 三到五 (区间)。
_SCORE = re.compile(r"(?<!\d)(\d{1,3})\s*[:：]\s*(\d{1,3})(?!\d)")
_RANGE = re.compile(r"(?<!\d)(\d{1,4})\s*[~～]\s*(\d{1,4})(?!\d)")
_RANGE_DASH = re.compile(r"(?<![\d.])(\d{1,4})\s*[-—]\s*(\d{1,4})(?![\d.])")


def _tn_symbols(text: str) -> str:
    text = _SCORE.sub(r"\1比\2", text)
    text = _RANGE.sub(r"\1到\2", text)
    text = _RANGE_DASH.sub(r"\1到\2", text)
    return text


# ---- datetime: 日期/时间归一化 ---------------------------------------------
# 只认强信号形态, 规避「日期 vs 区间/版本」歧义 (兜底: 拿不准就不改):
#   - 日期用 / 或 - 分隔且以 4 位年打头 (2024/3/5, 2024-03-05); 不碰 . (留给版本号),
#     不碰短形式 3/5、3-5 (与区间/分数歧义)。
#   - 时间 分/秒 必须两位 (00-59), 从而与比分 3:2 (分部 1 位) 天然区分。
# 放在 symbols 之前: 否则 2024-3-5 会被区间规则先吃成 "2024到3"。
# 月/日限定合法范围 (月 1-12, 日 1-31), 避免把年份区间 2024-25 误读成「2024年25月」。
_MON = r"(1[0-2]|0?[1-9])"
_DAY = r"(3[01]|[12]\d|0?[1-9])"
_DATE_YMD = re.compile(rf"(?<!\d)(\d{{4}})[/\-]{_MON}[/\-]{_DAY}(?!\d)")
_DATE_YM = re.compile(rf"(?<!\d)(\d{{4}})[/\-]{_MON}(?!\d)")
_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)")


def _strip_leading_zero(s: str) -> str:
    # "03"->"3"; "0"->"0" (保留单个 0)。让月/日读作「三月」而非「零三月」。
    return s.lstrip("0") or "0"


def _tn_datetime(text: str) -> str:
    def ymd(m: re.Match) -> str:
        return (
            f"{m.group(1)}年{_strip_leading_zero(m.group(2))}月"
            f"{_strip_leading_zero(m.group(3))}日"
        )

    def ym(m: re.Match) -> str:
        return f"{m.group(1)}年{_strip_leading_zero(m.group(2))}月"

    def hms(m: re.Match) -> str:
        out = f"{_strip_leading_zero(m.group(1))}时{_strip_leading_zero(m.group(2))}分"
        if m.group(3) is not None:
            out += f"{_strip_leading_zero(m.group(3))}秒"
        return out

    text = _DATE_YMD.sub(ymd, text)
    text = _DATE_YM.sub(ym, text)
    text = _TIME.sub(hms, text)
    return text


# ---- finance: 货币金额 ------------------------------------------------------
# 「薄层」定位: 不重造「数字转中文大写」(交给模型内置 TN), 只做模型不擅长的三件事:
#   1) 去千分位逗号 (1,234,567 -> 1234567), 避免模型在逗号处断读;
#   2) 货币符号 -> 词 (¥/￥->元, $->美元, €->欧元, £->英镑), 符号前缀转后缀单位;
#   3) 人民币小数按「元角分」读 (12.56元 -> 12元5角6分); 外币小数保留交模型 (9.99美元)。
# 放在 digit_string 之前 (金额是数量, 不该被逐位读)。注意: 若同时勾选 digit_string,
# 去逗号后的裸整数 (>=2 位) 会被逐位规则命中 —— 二者语义相反, 不建议同时勾选。
_CURRENCY_SYM = {"¥": "元", "￥": "元", "$": "美元", "€": "欧元", "£": "英镑"}
_MONEY_SYM = re.compile(r"([¥￥$€£])\s?(\d[\d,]*)(?:\.(\d{1,2}))?")
_MONEY_CNY = re.compile(r"(?<![\d.])(\d[\d,]*)(?:\.(\d{1,2}))?\s?(元|块钱|块)(?!\d)")


def _read_jiao_fen(dec: str) -> str:
    # dec: 1~2 位小数字符串 -> 角/分读法 (数字保留阿拉伯, 交模型读)。
    # ".00"/".0"/".05" 中的整数 0 位省略; 全 0 返回 "" (整元)。
    d1 = dec[0]
    d2 = dec[1] if len(dec) > 1 else ""
    out = ""
    if d1 != "0":
        out += d1 + "角"
    if d2 and d2 != "0":
        out += d2 + "分"
    return out


def _tn_finance(text: str) -> str:
    def sym(m: re.Match) -> str:
        unit = _CURRENCY_SYM[m.group(1)]
        intp = m.group(2).replace(",", "")
        dec = m.group(3)
        if unit == "元" and dec:
            jf = _read_jiao_fen(dec)
            return f"{intp}元{jf}" if jf else f"{intp}元"
        if dec:
            return f"{intp}.{dec}{unit}"
        return f"{intp}{unit}"

    def cny(m: re.Match) -> str:
        intp = m.group(1).replace(",", "")
        dec = m.group(2)
        if dec:
            jf = _read_jiao_fen(dec)
            return f"{intp}元{jf}" if jf else f"{intp}元"
        return f"{intp}{m.group(3)}"

    text = _MONEY_SYM.sub(sym, text)
    text = _MONEY_CNY.sub(cny, text)
    return text


# ---- abbrev: 大写缩写拼读 --------------------------------------------------
# 规则默认: 独立大写字母串 (2~6 位) 逐字母拼读 (FBI -> F B I, GDP -> G D P),
# 这是中文语境下缩写的高频读法。少数按单词读的缩写走极小静态白名单豁免 (原样留给
# 模型发音), 不引入词典依赖。数字+单字母的等级/规格 (3A/5G/4K) 只有 1 个字母,
# 不命中本规则, 保持原样交模型 (读作「三A / 五G」)。
_ACRONYM_WORDS = {"NASA", "NATO", "UNESCO", "UNICEF", "OPEC", "APEC", "SARS", "GIF"}
_ACRONYM = re.compile(r"(?<![A-Za-z])[A-Z]{2,6}(?![A-Za-z])")


def _tn_abbrev(text: str) -> str:
    def repl(m: re.Match) -> str:
        s = m.group(0)
        return s if s in _ACRONYM_WORDS else " ".join(s)

    return _ACRONYM.sub(repl, text)


_IMPL_FUNCS = {
    "datetime": _tn_datetime,
    "finance": _tn_finance,
    "symbols": _tn_symbols,
    "units": _tn_units,
    "version": _tn_version,
    "abbrev": _tn_abbrev,
    # digit_string 放最后: 前面几类把日期/金额/版本号/单位/比分里的数字先处理掉,
    # 避免它们被当作长数字串逐位读。
    "digit_string": _tn_digit_string,
}
# 固定执行顺序 (dict 有序, 但显式列出更清晰)。datetime 在 symbols 前 (防 2024-3-5
# 被区间吃掉); finance 在 digit_string 前 (金额是数量不逐位读)。
_ORDER = ["datetime", "finance", "symbols", "units", "version", "abbrev", "digit_string"]


def apply_domain_tn(text: str, categories: list[str] | None) -> str:
    """按用户选择的领域类别做归一化预处理 (送模型内置 TN 之前)。

    categories: 前端勾选的类别 id 列表; None/空 = 不做任何领域 TN (原样返回)。
    未知类别忽略; noop 类别 (需词典, 实验性) 当前原样透传。
    只有 _ORDER 中的已实现类别会真正改写文本, 且按固定顺序执行。
    """
    if not text or not categories:
        return text or ""
    selected = set(categories)
    for key in _ORDER:
        if key in selected:
            text = _IMPL_FUNCS[key](text)
    return text
