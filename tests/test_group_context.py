"""群聊上下文共享缓冲测试（临时库，覆盖隔离/持久化）。"""

import pytest

from src.shared import group_context


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path):
    """每个测试指向独立的临时库，并清空内存缓存。"""
    group_context.init(tmp_path / "ctx.db")
    yield


def test_append_and_recent():
    group_context.clear(1)
    group_context.append(1, "小明", "今天天气不错")
    group_context.append(1, "小红", "晚上吃火锅")
    assert group_context.recent(1, 5) == ["小明：今天天气不错", "小红：晚上吃火锅"]


def test_recent_caps_at_n():
    for i in range(10):
        group_context.append(2, f"u{i}", f"msg{i}")
    recent = group_context.recent(2, 3)
    assert recent == ["u7：msg7", "u8：msg8", "u9：msg9"]


def test_recent_zero_or_empty_group():
    assert group_context.recent(99, 5) == []
    assert group_context.recent(99, 0) == []


def test_append_truncates_long_text():
    group_context.append(3, "小明", "长" * 500)
    item = group_context.recent(3, 1)[0]
    assert len(item) <= 100 + len("小明：")


def test_append_sanitizes_speaker_name():
    """名片带 CQ 码/换行时进缓冲前洗掉（缓冲内容会拼进提示词）。"""
    group_context.append(4, "[CQ:at,qq=all]张\n三", "内容")
    assert group_context.recent(4, 1)[0] == "张 三：内容"


def test_append_all_dirty_name_falls_back():
    """洗完只剩空串的名字兜底成「某人」，不出「：内容」这种残行。"""
    group_context.append(5, "[CQ:at,qq=all]", "内容")
    assert group_context.recent(5, 1)[0] == "某人：内容"


def test_buffers_are_isolated_per_group():
    group_context.append(10, "A", "hello")
    assert group_context.recent(11, 5) == []


def test_survives_restart(tmp_path):
    """重启（内存缓存清空）后从库里懒加载回来。"""
    group_context.append(7, "小明", "重启前的消息")
    group_context.init(tmp_path / "ctx.db")  # 模拟重启：清内存、重连同一个库
    assert group_context.recent(7, 5) == ["小明：重启前的消息"]


def test_clear_wipes_memory_and_disk():
    group_context.append(8, "小明", "会话内容")
    group_context.clear(8)
    group_context.init()  # 再清一次内存，确认磁盘上也没了
    assert group_context.recent(8, 5) == []


def test_buffer_caps_at_maxlen():
    for i in range(group_context.MAXLEN + 10):
        group_context.append(9, "u", f"m{i}")
    lines = group_context.recent(9, group_context.MAXLEN + 5)
    assert len(lines) == group_context.MAXLEN
    assert lines[-1] == f"u：m{group_context.MAXLEN + 9}"
