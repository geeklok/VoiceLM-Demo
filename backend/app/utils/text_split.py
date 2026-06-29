from __future__ import annotations

# 主断句标点 (附在前句末尾): 中英文句末 + 分号 + 换行
_PRIMARY = "。！？!?；;\n"
# 次断句标点: 仅用于把超长句进一步切短
_SECONDARY = "，,、 "


def ends_with_sentence_punct(text: str) -> bool:
    """文本 (去尾部空白后) 是否以主断句标点结尾。

    流式聊天用: 判断 LLM 增量缓冲区当前是否落在一个完整句末,
    以决定能否把缓冲区整体送 TTS, 还是留下末段未完句继续等待。
    """
    s = (text or "").rstrip()
    return bool(s) and s[-1] in _PRIMARY


def split_sentences(text: str, max_chars: int = 60, min_chars: int = 0) -> list[str]:
    """按标点把文本分句, 让首句尽量短以降流式首包延迟。

    主标点切句; 超长句再按次标点 / 硬截断切到 max_chars 以内。
    min_chars>0 时: 切分后贪心合并过短段, 使每段尽量 >=min_chars ——
    避免短句音频盖不住下一段 LLM prefill 而出现句间播放间隙。
    strip 空白、丢空串; 空输入返回 []。
    """
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
    if min_chars > 0:
        out = _merge_short(out, min_chars, max_chars)
    return out


def _merge_short(segs: list[str], min_chars: int, max_chars: int) -> list[str]:
    """贪心合并过短段: 累积到 >=min_chars 才 flush; 但不超过 max_chars。

    保证最后一段也并入前段 (避免末尾留一个孤短段)。
    """
    if not segs:
        return segs
    merged: list[str] = []
    buf = ""
    for s in segs:
        if not buf:
            buf = s
        elif len(buf) + len(s) <= max_chars:
            buf += s
        else:
            merged.append(buf)
            buf = s
        if len(buf) >= min_chars:
            merged.append(buf)
            buf = ""
    if buf:
        if merged and len(merged[-1]) + len(buf) <= max_chars:
            merged[-1] += buf
        else:
            merged.append(buf)
    return merged


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
