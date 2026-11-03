"""共享群历史拉取模块测试：过滤规则、按天过滤、失败语义（None vs []）。"""

import asyncio

from src.shared import history


def _msg(mid, uid, text, ts=None, card=None):
    m = {
        "message_id": mid,
        "sender": {"user_id": uid, **({"card": card} if card else {})},
        "message": [{"type": "text", "data": {"text": text}}],
    }
    if ts is not None:
        m["time"] = ts
    return m


class FakeBot:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def call_api(self, api, **params):
        self.calls.append((api, params))
        if api == "get_group_msg_history":
            return {"messages": self.messages}
        raise RuntimeError(api)


def test_fetch_filters_bot_self_commands_and_empty():
    bot = FakeBot([
        _msg(1, 888, "我是bot自己", card="机器人"),
        _msg(2, 1, "/news 命令"),
        _msg(3, 1, "  /斜杠空格也算命令"),
        _msg(4, 2, "正常聊天"),
        {"message_id": 5, "sender": {"user_id": 3}, "message": []},  # 无文字段
    ])
    lines = asyncio.run(history.fetch_group_lines(bot, 10000, 888, 20))
    # 无 card/nickname 的发送者回退用 QQ 号当名字
    assert lines == ["2：正常聊天"]


def test_fetch_media_placeholder():
    bot = FakeBot([
        {"message_id": 1, "sender": {"user_id": 1, "card": "小明"},
         "message": [{"type": "image", "data": {}}, {"type": "text", "data": {"text": "配图"}}]},
    ])
    lines = asyncio.run(history.fetch_group_lines(bot, 1, 99, 10))
    assert lines == ["小明：[图片]配图"]


def test_fetch_since_ts_filters_old_messages():
    bot = FakeBot([
        _msg(1, 1, "昨天的消息", ts=1000),
        _msg(2, 1, "今天的消息", ts=5000),
        _msg(3, 1, "没有时间戳的消息"),
    ])
    lines = asyncio.run(history.fetch_group_lines(bot, 1, 99, 10, since_ts=2000))
    assert lines == ["1：今天的消息"]


def test_fetch_excludes_message_id():
    bot = FakeBot([_msg(1, 1, "命令本身"), _msg(2, 2, "别的消息")])
    lines = asyncio.run(history.fetch_group_lines(bot, 1, 99, 10, exclude_message_id=1))
    assert lines == ["2：别的消息"]


def test_fetch_failure_returns_none():
    class BadBot:
        async def call_api(self, api, **params):
            raise RuntimeError("api not supported")

    assert asyncio.run(history.fetch_group_lines(BadBot(), 1, 2, 5)) is None


def test_fetch_quiet_day_returns_empty_list():
    bot = FakeBot([])
    assert asyncio.run(history.fetch_group_lines(bot, 1, 2, 5)) == []


def test_activity_tally_counts_and_filters():
    bot = FakeBot([
        _msg(1, 888, "我是bot自己", card="机器人"),
        _msg(2, 1, "/rank 命令"),
        _msg(3, 1, "第一条", card="小明"),
        {"message_id": 4, "sender": {"user_id": 1, "card": "小明"},
         "message": [{"type": "image", "data": {}}]},  # 纯媒体也算水群
        _msg(5, 2, "群友乙", card="阿乙"),
    ])
    tally = asyncio.run(history.fetch_group_activity(bot, 1, 888, 50))
    assert tally[1] == {"nickname": "小明", "msgs": 2}
    assert tally[2] == {"nickname": "阿乙", "msgs": 1}


def test_activity_since_ts_and_failure():
    bot = FakeBot([
        _msg(1, 1, "昨天", ts=1000),
        _msg(2, 1, "今天", ts=5000),
    ])
    tally = asyncio.run(history.fetch_group_activity(bot, 1, 99, 50, since_ts=2000))
    assert list(tally) == [1] and tally[1]["msgs"] == 1

    class BadBot:
        async def call_api(self, api, **params):
            raise RuntimeError("api not supported")

    assert asyncio.run(history.fetch_group_activity(BadBot(), 1, 2, 5)) is None
