"""一言插件的报文格式化测试。"""

from src.plugins.quote import format_quote


def test_format_quote_full():
    card = format_quote(
        {
            "hitokoto": "余生向阳，往后如意。",
            "type": "e",
            "from": "源初幻想",
            "from_who": "鹤空流乂",
        }
    )
    assert card == "「余生向阳，往后如意。」\n—— 鹤空流乂《源初幻想》 · 原创"


def test_format_quote_source_only():
    card = format_quote({"hitokoto": "句子", "type": "d", "from": "某本书", "from_who": ""})
    assert card == "「句子」\n—— 《某本书》 · 文学"


def test_format_quote_dedup_source_author():
    card = format_quote({"hitokoto": "句子", "from": "网易云", "from_who": "网易云"})
    assert card == "「句子」\n—— 《网易云》"


def test_format_quote_unknown_type_and_no_source():
    card = format_quote({"hitokoto": "句子", "type": "z"})
    assert card == "「句子」"


def test_format_quote_empty_text():
    assert format_quote({"hitokoto": "  "}) is None
    assert format_quote({}) is None
