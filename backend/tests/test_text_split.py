from __future__ import annotations

from app.utils.text_split import split_sentences


def test_empty_returns_empty():
    assert split_sentences("") == []
    assert split_sentences("   ") == []
    assert split_sentences(None) == []  # type: ignore[arg-type]


def test_single_sentence_not_split():
    assert split_sentences("你好世界") == ["你好世界"]


def test_split_on_primary_punctuation():
    out = split_sentences("你好。今天天气不错！出门吧？")
    assert out == ["你好。", "今天天气不错！", "出门吧？"]


def test_first_sentence_is_short():
    text = "你好。" + "啊" * 200
    out = split_sentences(text, max_chars=60)
    assert out[0] == "你好。"
    assert len(out[0]) < len(out[1])


def test_long_sentence_hard_capped():
    text = "啊" * 200  # 无标点
    out = split_sentences(text, max_chars=60)
    assert all(len(p) <= 60 for p in out)
    assert "".join(out) == text


def test_long_sentence_split_on_secondary():
    text = "甲" * 50 + "，" + "乙" * 50 + "，" + "丙" * 50
    out = split_sentences(text, max_chars=60)
    assert all(len(p) <= 60 for p in out)
    assert len(out) >= 2


def test_min_chars_zero_keeps_fine_split():
    # min_chars 默认 0: 行为与不传一致, 短句不合并
    text = "你好。今天天气不错！出门吧？"
    assert split_sentences(text, max_chars=60, min_chars=0) == [
        "你好。", "今天天气不错！", "出门吧？"
    ]


def test_min_chars_merges_short_segments():
    # 短句被向后合并到 >=min_chars, 段数变少, 内容与顺序不变
    text = "你好。今天天气很好。我们一起去公园散步吧。路上可以聊聊最近的新闻。然后再找家餐厅吃饭。"
    fine = split_sentences(text, max_chars=60, min_chars=0)
    merged = split_sentences(text, max_chars=60, min_chars=20)
    assert len(merged) < len(fine)
    assert "".join(merged) == "".join(fine)
    # 除可能的末段外, 每段都达到了 min_chars
    assert all(len(s) >= 20 for s in merged[:-1])


def test_min_chars_respects_max_chars():
    # 合并不得超过 max_chars
    text = "甲甲甲甲甲。乙乙乙乙乙。丙丙丙丙丙。丁丁丁丁丁。"
    out = split_sentences(text, max_chars=12, min_chars=10)
    assert all(len(s) <= 12 for s in out)
    assert "".join(out) == "".join(split_sentences(text, max_chars=12, min_chars=0))


def test_min_chars_last_short_segment_merged_back():
    # 末尾孤短段并入前段, 不留单独的极短尾巴
    text = "这是一段足够长的句子用来测试合并行为。好。"
    out = split_sentences(text, max_chars=60, min_chars=20)
    assert out[-1] != "好。"
    assert "".join(out) == "这是一段足够长的句子用来测试合并行为。好。"
