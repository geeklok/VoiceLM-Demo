from __future__ import annotations

from app.utils.text_clean import strip_markdown


def test_empty_and_none():
    assert strip_markdown("") == ""
    assert strip_markdown(None) == ""  # type: ignore[arg-type]


def test_plain_text_unchanged():
    assert strip_markdown("你好世界，今天天气不错。") == "你好世界，今天天气不错。"


def test_bold_italic_stripped():
    assert strip_markdown("这是**重点**内容") == "这是重点内容"
    assert strip_markdown("这是*斜体*和_下划线_") == "这是斜体和下划线"
    assert strip_markdown("这是~~删除线~~文本") == "这是删除线文本"


def test_headings_stripped():
    assert strip_markdown("# 一级标题") == "一级标题"
    assert strip_markdown("### 三级标题") == "三级标题"


def test_inline_code_stripped():
    assert strip_markdown("运行 `pip install` 命令") == "运行 pip install 命令"


def test_code_fence_keeps_inner():
    out = strip_markdown("```python\nprint(1)\n```")
    assert "```" not in out
    assert "print(1)" in out


def test_link_keeps_text_drops_url():
    out = strip_markdown("详见 [官方文档](https://example.com/docs) 说明")
    assert "官方文档" in out
    assert "example.com" not in out
    assert "http" not in out


def test_image_removed():
    out = strip_markdown("看图 ![截图](https://example.com/a.png) 结束")
    assert "截图" not in out
    assert "example.com" not in out
    assert "看图" in out and "结束" in out


def test_bare_url_removed():
    out = strip_markdown("访问 https://example.com/a/b?x=1 查看")
    assert "http" not in out
    assert "example.com" not in out
    assert "访问" in out and "查看" in out


def test_unordered_list_marker_removed():
    out = strip_markdown("- 第一项\n- 第二项")
    assert out == "第一项\n第二项"


def test_ordered_list_marker_removed():
    out = strip_markdown("1. 第一步\n2. 第二步")
    assert out == "第一步\n第二步"


def test_quote_marker_removed():
    assert strip_markdown("> 这是引用") == "这是引用"


def test_table_pipes_to_pause():
    out = strip_markdown("| 姓名 | 年龄 |\n| 张三 | 20 |")
    assert "|" not in out
    assert "姓名" in out and "张三" in out


def test_table_separator_row_removed():
    md = "| 姓名 | 年龄 |\n| --- | --- |\n| 张三 | 20 |"
    out = strip_markdown(md)
    assert "---" not in out
    assert "姓名" in out and "张三" in out


def test_mixed_document():
    md = (
        "## 结论\n"
        "**CosyVoice** 表现*不错*, 见 [链接](http://a.com)。\n"
        "- 支持流式\n"
        "- 首包 `150ms`"
    )
    out = strip_markdown(md)
    for junk in ("#", "*", "`", "[", "]", "(", ")", "http", "-"):
        assert junk not in out, f"{junk!r} still present in {out!r}"
    assert "结论" in out and "CosyVoice" in out and "支持流式" in out
