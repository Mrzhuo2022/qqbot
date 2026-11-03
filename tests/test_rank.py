"""水群排行插件测试：文案格式化（奖牌、占比、周期、空榜）。"""

from src.plugins.rank import format_rank


def _rows():
    return [
        {"user_id": 1, "nickname": "甲", "msgs": 50},
        {"user_id": 2, "nickname": "乙", "msgs": 25},
        {"user_id": 3, "nickname": "丙", "msgs": 25},
    ]


def test_format_rank_medals_and_totals():
    text = format_rank(_rows(), 100, days=1)
    assert "今日水群排行" in text
    assert "共 100 条消息" in text
    assert "🥇 甲：50 条" in text
    assert "🥈 乙：25 条" in text
    assert "🥉 丙：25 条" in text
    assert "贡献了本群 50%" in text  # 50/100


def test_format_rank_beyond_medals_uses_numbers():
    rows = _rows() + [{"user_id": i, "nickname": f"路人{i}", "msgs": 1} for i in range(4, 8)]
    text = format_rank(rows, 103, days=1)
    assert "4. 路人4" in text and "7. 路人7" in text


def test_format_rank_week_scope():
    assert "近 7 天水群排行" in format_rank(_rows(), 100, days=7)


def test_format_rank_empty():
    assert "还没有聊天记录" in format_rank([], 0, days=1)
