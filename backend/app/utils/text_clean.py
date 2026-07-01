from __future__ import annotations

import re

# 代码块 ```lang ... ```: 去掉围栏, 保留内部代码文本 (让其正常朗读)
_FENCE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
# 图片 ![alt](url): 整体删除 (alt 一般无朗读价值, url 更不该念)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 链接 [text](url): 保留 text, 丢 url
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 裸 URL: http(s):// 或 www. 开头, 念出来是噪声
_BARE_URL = re.compile(r"(?:https?://|www\.)\S+")
# 行首标记: #### 标题 / > 引用 / 无序列表 -*+ / 有序列表 1. 2)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>+\s?", re.MULTILINE)
_UL = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_OL = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
# 表格分隔行 |---|:--:|
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.MULTILINE)
# 行内强调/代码符号: ** __ * _ ~~ `
_EMPHASIS = re.compile(r"(\*\*|__|~~|\*|_|`)")
# 连续空白折叠
_WS = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{2,}")


def strip_markdown(text: str) -> str:
    """把 markdown 文本转成适合 TTS 朗读的纯文本。

    CosyVoice 的 text_normalize 只做 TN (数字读法/标点规整), 不剥离 markdown 语法,
    于是 #*_`、[]()、竖线、裸 URL 会被逐字念出或打乱韵律 (见实锤)。此函数在送引擎前
    去掉标记语法、保留可朗读内容: 链接留文字丢 URL、删图片与裸 URL、行首标记转停顿、
    表格竖线转停顿。引擎无关, file 与 stream 两条 TTS 路径共用。
    """
    if not text:
        return text or ""

    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _FENCE.sub(lambda m: m.group(1), t)
    t = _IMAGE.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _BARE_URL.sub("", t)
    t = _TABLE_SEP.sub("", t)
    t = _HEADING.sub("", t)
    t = _QUOTE.sub("", t)
    t = _UL.sub("", t)
    t = _OL.sub("", t)
    t = _EMPHASIS.sub("", t)
    # 表格单元格竖线转停顿, 避免 "|姓名|年龄|" 连读
    t = t.replace("|", "，")
    t = _WS.sub(" ", t)
    t = _BLANK_LINES.sub("\n", t)

    lines = [ln.strip().strip("，").strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()
