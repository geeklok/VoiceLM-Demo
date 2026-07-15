from __future__ import annotations

from app.utils.domain_tn import (
    DOMAIN_TN_CATEGORIES,
    IMPLEMENTED,
    apply_default_tts_tn,
    apply_domain_tn,
)


# ---- 空/边界 --------------------------------------------------------------

def test_none_and_empty_categories_passthrough():
    assert apply_domain_tn("验证码 123456", None) == "验证码 123456"
    assert apply_domain_tn("验证码 123456", []) == "验证码 123456"
    assert apply_domain_tn("", ["digit_string"]) == ""


def test_unknown_category_ignored():
    # 未知类别不报错, 原样返回。
    assert apply_domain_tn("你好", ["not_a_real_category"]) == "你好"


def test_default_tts_tn_bare_number_to_chinese_quantity():
    # 不勾任何领域 TN 时，纯数字不能因为缺中文上下文被模型读成英文 Twelve Thirty-four。
    assert apply_default_tts_tn("1234") == "一千二百三十四"
    assert apply_default_tts_tn("1,234.50") == "一千二百三十四点五零"
    # 前导 0 更像编号，逐位保留 0。
    assert apply_default_tts_tn("0012") == "零零一二"


def test_default_tts_tn_only_handles_whole_numeric_text():
    # 句子里的数字仍交给模型内置 TN；验证码/号码逐位读由显式 digit_string 负责。
    assert apply_default_tts_tn("验证码 1234") == "验证码 1234"
    assert apply_default_tts_tn("09:30") == "09:30"


# ---- digit_string: 逐位读 -------------------------------------------------

def test_digit_string_reads_long_number_per_digit():
    out = apply_domain_tn("验证码是123456，请查收", ["digit_string"])
    # 6 位串逐位读: 1->幺 2->二 ... 空格分隔
    assert "幺 二 三 四 五 六" in out
    assert "123456" not in out


def test_digit_string_reads_short_number_per_digit():
    # digit_string 是显式开关: 勾选后 >=2 位数字串一律逐位读 (含 4 位 1234)。
    # 修复前阈值 >=6 会漏掉 1234, 被模型当年份读成英文 "Twelve Thirty-four"。
    out = apply_domain_tn("验证码 1234", ["digit_string"])
    assert "幺 二 三 四" in out
    assert "1234" not in out


def test_digit_string_keeps_single_digit():
    # 孤立单个数字不拆 (既非「串」, 逐位读==数量读), 避免「第3章」「买1送1」被拆。
    out = apply_domain_tn("我有3个苹果", ["digit_string"])
    assert "3" in out
    assert "三" not in out


def test_digit_string_phone_with_separators():
    out = apply_domain_tn("电话 138-1234-5678 打过来", ["digit_string"])
    assert "幺 三 八 幺 二 三 四 五 六 七 八" in out


# ---- units: 计量单位 ------------------------------------------------------

def test_units_map_symbols():
    out = apply_domain_tn("时速 60km/h，水温 25℃", ["units"])
    assert "千米每小时" in out
    assert "摄氏度" in out
    assert "km/h" not in out


def test_units_longest_match_first():
    # km/h 应整体命中, 不被 km 先替换成 "千米/h"。
    out = apply_domain_tn("60km/h", ["units"])
    assert out == "60千米每小时"


# ---- version: 版本号逐段 --------------------------------------------------

def test_version_reads_segments_with_dot():
    out = apply_domain_tn("升级到 v2.3.1 版本", ["version"])
    assert "版本 2 点 3 点 1" in out
    assert "v2.3.1" not in out


def test_version_leaves_plain_decimal():
    # 单个小数 (一个点) 不是版本号, 不动, 交给模型读小数。
    out = apply_domain_tn("价格 3.5 元", ["version"])
    assert "3.5" in out


# ---- symbols: 比分/区间 ---------------------------------------------------

def test_symbols_score_and_range():
    assert apply_domain_tn("比分 3:2", ["symbols"]) == "比分 3比2"
    assert apply_domain_tn("大约 3~5 个", ["symbols"]) == "大约 3到5 个"


# ---- noop 类别 (需词典, 实验性) 透传 --------------------------------------

def test_noop_categories_passthrough():
    for cat in ("medical", "address", "legal", "name"):
        assert DOMAIN_TN_CATEGORIES[cat][0] is False
        # noop 类别当前不改写文本。
        assert apply_domain_tn("阿司匹林 0.5g bid", [cat]) == "阿司匹林 0.5g bid"


# ---- datetime: 日期/时间 --------------------------------------------------

def test_datetime_ymd():
    out = apply_domain_tn("发布于 2024/3/5 上午", ["datetime"])
    assert "2024年3月5日" in out
    assert "2024/3/5" not in out


def test_datetime_ymd_dash_and_zero_padded():
    out = apply_domain_tn("截止 2024-03-05", ["datetime"])
    assert "2024年3月5日" in out


def test_datetime_year_month_only():
    out = apply_domain_tn("2024-12 财报", ["datetime"])
    assert "2024年12月" in out


def test_datetime_time_hms():
    out = apply_domain_tn("会议 09:30 开始，14:05:08 结束", ["datetime"])
    assert "9时30分" in out
    assert "14时5分8秒" in out


def test_datetime_ignores_year_range():
    # 年份区间 2020-2024 不是日期, 月份越界, 不应被改。
    assert apply_domain_tn("2020-2024 五年", ["datetime"]) == "2020-2024 五年"


def test_datetime_before_symbols_no_range_capture():
    # datetime 先于 symbols: 2024-3-5 不会被区间规则吃成 "2024到3"。
    out = apply_domain_tn("日期 2024-3-5", ["datetime", "symbols"])
    assert "2024年3月5日" in out
    assert "到" not in out


# ---- finance: 货币金额 ----------------------------------------------------

def test_finance_cny_symbol_with_decimal():
    out = apply_domain_tn("总价 ¥12.56", ["finance"])
    assert "12元5角6分" in out


def test_finance_thousands_separator_stripped():
    out = apply_domain_tn("成交 1,234,567元", ["finance"])
    assert "1234567元" in out
    assert "," not in out


def test_finance_integer_cny_unchanged_amount():
    # 整元无小数: 去符号/逗号即可, 数量读法交模型。
    assert apply_domain_tn("￥100", ["finance"]) == "100元"


def test_finance_foreign_currency_keeps_decimal():
    out = apply_domain_tn("售价 $9.99", ["finance"])
    assert "9.99美元" in out


def test_finance_zero_fen_omitted():
    out = apply_domain_tn("¥5.00 一杯", ["finance"])
    assert "5元" in out
    assert "角" not in out and "分" not in out


# ---- abbrev: 大写缩写拼读 -------------------------------------------------

def test_abbrev_spells_out_letters():
    out = apply_domain_tn("FBI 和 GDP 数据", ["abbrev"])
    assert "F B I" in out
    assert "G D P" in out


def test_abbrev_whitelist_read_as_word():
    out = apply_domain_tn("NASA 发射", ["abbrev"])
    assert "NASA" in out
    assert "N A S A" not in out


def test_abbrev_ignores_grade_specs():
    # 3A/5G 只有 1 个字母, 不逐字母拆。
    out = apply_domain_tn("3A 景区 5G 网络", ["abbrev"])
    assert "3A" in out and "5G" in out


# ---- 组合 + 顺序 ----------------------------------------------------------

def test_combined_version_before_digit_string():
    # 版本号先处理, 其数字不应被 digit_string 逐位拆。
    out = apply_domain_tn("装 v1.2.3 后拨 400888", ["version", "digit_string"])
    assert "版本 1 点 2 点 3" in out
    assert "四 零 零 八 八 八" in out


def test_finance_alone_keeps_amount_as_quantity():
    # finance 单独勾选: 金额是数量, 去符号后整体保留交模型按数量读, 不逐位拆。
    out = apply_domain_tn("余额 ￥12345", ["finance"])
    assert "12345元" in out


def test_finance_with_digit_string_conflicts():
    # finance 与 digit_string 语义相反 (数量 vs 逐位), 同勾时 digit_string 会把
    # 金额也逐位拆 —— 文档 §5.7.1 已注明不建议同勾, 此处锁定该冲突行为不被误判为 bug。
    out = apply_domain_tn("余额 ￥12345", ["finance", "digit_string"])
    assert "幺 二 三 四 五" in out


def test_implemented_set_matches_registry():
    impl = tuple(k for k, (ok, _) in DOMAIN_TN_CATEGORIES.items() if ok)
    assert set(IMPLEMENTED) == set(impl)
    assert set(IMPLEMENTED) == {
        "digit_string", "units", "version", "symbols",
        "datetime", "finance", "abbrev",
    }
