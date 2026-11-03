"""群聊晚报插件测试：生成门槛、素材时间过滤、推送去重与群级开关。"""

import asyncio
import datetime as dt

from src.plugins import group_digest as gd
from src.shared.config import plugin_config

TS_TODAY = int(dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()) + 3600
TS_YESTERDAY = TS_TODAY - 86400


def _msg(mid, uid, text, ts=TS_TODAY):
    return {
        "message_id": mid,
        "time": ts,
        "sender": {"user_id": uid, "card": f"用户{uid}"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


class FakeBot:
    def __init__(self, messages, self_id=888, send_ok=True):
        self.self_id = self_id
        self._messages = messages
        self._send_ok = send_ok
        self.sent = []

    async def call_api(self, api, **params):
        if api == "get_group_msg_history":
            return {"messages": self._messages}
        if api == "send_group_msg":
            if not self._send_ok:
                raise RuntimeError("send failed")
            self.sent.append(params)
            return {"message_id": len(self.sent)}
        raise RuntimeError(api)


def _patch_llm(monkeypatch, reply="【今日话题】大家聊了测试。", ready=True, fail=False):
    captured = {}

    async def fake_chat(system, prompt, *args, **kwargs):
        if fail:
            raise RuntimeError("llm down")
        captured["system"], captured["prompt"] = system, prompt
        return reply

    monkeypatch.setattr(gd.llm, "llm_ready", lambda: ready)
    monkeypatch.setattr(gd.llm, "llm_chat", fake_chat)
    return captured


def test_build_digest_ok(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_min_messages", 3)
    captured = _patch_llm(monkeypatch, reply="【今日话题】聊了聊晚报。")
    bot = FakeBot([_msg(i, i, f"消息{i}") for i in range(1, 4)])
    digest, why = asyncio.run(gd.build_digest(bot, 10000))
    assert why == "ok"
    assert gd._today() in digest
    assert "【今日话题】聊了聊晚报。" in digest
    # 素材按不可信数据边界交给 LLM
    assert "<untrusted_data>" in captured["prompt"]
    assert "用户1：消息1" in captured["prompt"]


def test_build_digest_quiet_today(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_min_messages", 3)
    captured = _patch_llm(monkeypatch, fail=True)  # 不该走到 LLM
    bot = FakeBot([
        _msg(1, 1, "今天的话", ts=TS_TODAY),
        _msg(2, 2, "昨天的话", ts=TS_YESTERDAY),
        _msg(3, 3, "更昨天的话", ts=TS_YESTERDAY),
    ])
    digest, why = asyncio.run(gd.build_digest(bot, 10000))
    assert (digest, why) == (None, "quiet")
    assert "prompt" not in captured


def test_build_digest_llm_not_ready(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_min_messages", 1)
    _patch_llm(monkeypatch, ready=False)
    bot = FakeBot([_msg(1, 1, "话")])
    digest, why = asyncio.run(gd.build_digest(bot, 10000))
    assert (digest, why) == (None, "llm")


def test_build_digest_llm_error(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_min_messages", 1)
    _patch_llm(monkeypatch, fail=True)
    bot = FakeBot([_msg(1, 1, "话")])
    digest, why = asyncio.run(gd.build_digest(bot, 10000))
    assert (digest, why) == (None, "error")


def test_collect_today_falls_back_to_buffer(monkeypatch):
    class DeadApiBot:
        self_id = 888

        async def call_api(self, api, **params):
            raise RuntimeError("no history api")

    monkeypatch.setattr(gd.group_context, "recent", lambda gid, n: [f"用户1：缓冲{n}"])
    lines = asyncio.run(gd._collect_today(DeadApiBot(), 10000))
    assert lines == ["用户1：缓冲100"]


def test_push_digest_marks_pushed_only_when_delivered(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_groups", [111, 222])
    saved: dict = {}
    monkeypatch.setattr(gd, "get_setting", lambda k, d=None: None)
    monkeypatch.setattr(gd, "set_setting", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(gd, "group_plugin_enabled_id", lambda g, p: True)

    async def fake_digest(bot, gid):
        return ("🌙 群聊晚报", "ok")

    async def nosleep(_):
        return None

    monkeypatch.setattr(gd, "build_digest", fake_digest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)

    monkeypatch.setattr(gd, "get_bot", lambda: FakeBot([], send_ok=False))
    result = asyncio.run(gd.push_digest(force=True))
    assert "0/2" in result
    assert "digest:last_push" not in saved

    ok_bot = FakeBot([])
    monkeypatch.setattr(gd, "get_bot", lambda: ok_bot)
    result = asyncio.run(gd.push_digest(force=True))
    assert "2/2" in result
    assert saved.get("digest:last_push") == gd._today()
    assert [p["group_id"] for p in ok_bot.sent] == [111, 222]


def test_push_digest_skips_disabled_group(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_groups", [111, 222])
    monkeypatch.setattr(gd, "get_setting", lambda k, d=None: None)
    monkeypatch.setattr(gd, "set_setting", lambda k, v: None)
    # 群 222 关掉了晚报插件
    monkeypatch.setattr(gd, "group_plugin_enabled_id", lambda g, p: g != 222)

    async def fake_digest(bot, gid):
        return ("🌙 群聊晚报", "ok")

    async def nosleep(_):
        return None

    monkeypatch.setattr(gd, "build_digest", fake_digest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    bot = FakeBot([])
    monkeypatch.setattr(gd, "get_bot", lambda: bot)
    result = asyncio.run(gd.push_digest(force=True))
    assert "1/1" in result
    assert [p["group_id"] for p in bot.sent] == [111]


def test_push_digest_dedup_today(monkeypatch):
    monkeypatch.setattr(gd, "get_setting", lambda k, d=None: gd._today())
    result = asyncio.run(gd.push_digest())
    assert "已经推过" in result


def test_reason_text_covers_all_failures():
    for why in ("quiet", "llm", "error"):
        assert gd.REASON_TEXT[why]


# ── 定时推送失败自动重试 ──────────────────────────────────

def _patch_retry_env(monkeypatch, push_result):
    scheduled = []

    async def fake_push(force=False):
        return push_result

    monkeypatch.setattr(gd, "_push_digest", fake_push)
    monkeypatch.setattr(
        gd.scheduler, "add_job",
        lambda *a, **k: scheduled.append((k.get("args"), k.get("id"))),
    )
    return scheduled


def test_auto_digest_retries_when_failed(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (0, 2, "晚报已推送到 0/2 个群，2 个群失败"))
    asyncio.run(gd._auto_digest())
    assert scheduled == [([1], "group_digest_retry")]


def test_auto_digest_no_retry_for_quiet_or_pushed(monkeypatch):
    # 全部安静跳过：不是故障，不重试
    scheduled = _patch_retry_env(monkeypatch, (0, 0, "晚报已推送到 0/2 个群，2 个群今天素材不够"))
    asyncio.run(gd._auto_digest())
    assert scheduled == []

    # 已推过 / 部分成功
    scheduled = _patch_retry_env(monkeypatch, (-1, 0, "今天已经推过了"))
    asyncio.run(gd._auto_digest())
    assert scheduled == []

    scheduled = _patch_retry_env(monkeypatch, (1, 1, "晚报已推送到 1/2 个群，1 个群失败"))
    asyncio.run(gd._auto_digest())
    assert scheduled == []


def test_auto_digest_stops_at_retry_cap(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (0, 2, "全失败"))
    asyncio.run(gd._auto_digest(retries=gd.PUSH_RETRIES))
    assert scheduled == []


def test_push_digest_summary_separates_quiet_and_failed(monkeypatch):
    monkeypatch.setattr(plugin_config, "digest_groups", [111, 222])
    monkeypatch.setattr(gd, "get_setting", lambda k, d=None: None)
    monkeypatch.setattr(gd, "set_setting", lambda k, v: None)
    monkeypatch.setattr(gd, "group_plugin_enabled_id", lambda g, p: True)

    async def fake_digest(bot, gid):
        return (None, "quiet") if gid == 111 else (None, "llm")

    async def nosleep(_):
        return None

    monkeypatch.setattr(gd, "build_digest", fake_digest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(gd, "get_bot", lambda: FakeBot([]))
    result = asyncio.run(gd.push_digest(force=True))
    assert "0/2" in result and "1 个群今天素材不够" in result and "1 个群失败" in result
