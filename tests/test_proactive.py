"""主动回复插件测试：静音时段、冷却门、跳过识别、发送管线。"""

import asyncio
import datetime as dt
import time
from types import SimpleNamespace

from src.plugins import proactive as pc
from src.shared.config import plugin_config


class FakeBot:
    def __init__(self, send_ok=True):
        self.sent = []
        self._send_ok = send_ok

    async def call_api(self, api, **params):
        if api == "send_group_msg":
            if not self._send_ok:
                raise RuntimeError("send failed")
            self.sent.append(params)
            return {"message_id": len(self.sent)}
        raise RuntimeError(api)


def _event(gid=42, mid=1001):
    # proactive_speak 只用到 group_id / message_id
    return SimpleNamespace(group_id=gid, message_id=mid)


def _patch_llm(monkeypatch, reply="哈哈这也太离谱了", ready=True, fail=False):
    captured = {}

    async def fake_chat(system, prompt, *args, **kwargs):
        if fail:
            raise RuntimeError("llm down")
        captured["system"], captured["prompt"] = system, prompt
        return reply

    monkeypatch.setattr(pc.llm, "llm_ready", lambda: ready)
    monkeypatch.setattr(pc.llm, "llm_chat", fake_chat)
    return captured


def test_quiet_hours_same_day_and_wrap(monkeypatch):
    monkeypatch.setattr(plugin_config, "proactive_quiet_start", "12:00")
    monkeypatch.setattr(plugin_config, "proactive_quiet_end", "14:00")
    assert pc._quiet_now(dt.time(13, 0))
    assert not pc._quiet_now(dt.time(11, 59))
    assert not pc._quiet_now(dt.time(14, 0))  # 止点本身不静音

    # 起 > 止按跨午夜算：23:30–07:00 覆盖 0 点
    monkeypatch.setattr(plugin_config, "proactive_quiet_start", "23:30")
    monkeypatch.setattr(plugin_config, "proactive_quiet_end", "07:00")
    assert pc._quiet_now(dt.time(23, 59))
    assert pc._quiet_now(dt.time(0, 0))
    assert pc._quiet_now(dt.time(6, 59))
    assert not pc._quiet_now(dt.time(7, 0))
    assert not pc._quiet_now(dt.time(12, 0))


def test_quiet_hours_disabled(monkeypatch):
    # 起止留空或相等 = 不静音
    monkeypatch.setattr(plugin_config, "proactive_quiet_start", "")
    monkeypatch.setattr(plugin_config, "proactive_quiet_end", "")
    assert not pc._quiet_now(dt.time(2, 0))
    monkeypatch.setattr(plugin_config, "proactive_quiet_start", "01:00")
    monkeypatch.setattr(plugin_config, "proactive_quiet_end", "01:00")
    assert not pc._quiet_now(dt.time(1, 30))


def test_cooldown_gate(monkeypatch):
    monkeypatch.setattr(plugin_config, "proactive_cooldown", 600)
    pc._last_speak.clear()
    assert pc._cooldown_ok(1)
    pc._last_speak[1] = time.monotonic()
    assert not pc._cooldown_ok(1)
    # 冷却是按群隔离的
    assert pc._cooldown_ok(2)
    # 时间戳可注入，冷却过期后放行
    assert pc._cooldown_ok(1, now=time.monotonic() + 601)


def test_pick_reply_skip_variants():
    for raw in ("", "   ", "[SKIP]", "[skip]", "[跳过]", "[SKIP] 还是算了", "先 [SKIP] 吧"):
        assert pc._pick_reply(raw) is None, raw
    assert pc._pick_reply("哈哈哈这也太绝了") == "哈哈哈这也太绝了"
    assert pc._pick_reply(None) is None


def test_pick_reply_clips_long():
    long = "很长的消息" * 50
    out = pc._pick_reply(long)
    assert out is not None and len(out) == 101 and out.endswith("…")


def test_proactive_speak_sends_with_quote(monkeypatch):
    captured = _patch_llm(monkeypatch, reply="这歌是周杰伦的吧")
    recorded = []
    monkeypatch.setattr(
        pc.group_context, "append", lambda gid, who, text: recorded.append((gid, who, text))
    )
    bot = FakeBot()
    lines = [f"用户{i}：消息{i}" for i in range(5)]
    sent, why = asyncio.run(pc.proactive_speak(bot, _event(42, 1001), lines))
    assert (sent, why) == ("这歌是周杰伦的吧", "ok")
    # 带引用回复触发的那条消息
    assert len(bot.sent) == 1
    params = bot.sent[0]
    assert params["group_id"] == 42
    assert str(params["message"]) == "[CQ:reply,id=1001]这歌是周杰伦的吧"
    # 自己说的话记进群上下文，后续对话/主动回复不会自言自语
    assert recorded == [(42, "我", "这歌是周杰伦的吧")]
    # 聊天记录按不可信数据边界交给 LLM
    assert "<untrusted_data>" in captured["prompt"]
    assert "用户1：消息1" in captured["prompt"]


def test_proactive_speak_skip(monkeypatch):
    _patch_llm(monkeypatch, reply="[SKIP]")
    bot = FakeBot()
    sent, why = asyncio.run(
        pc.proactive_speak(bot, _event(), ["a：1", "b：2", "c：3"])
    )
    assert (sent, why) == (None, "skip")
    assert bot.sent == []


def test_proactive_speak_needs_context(monkeypatch):
    captured = _patch_llm(monkeypatch, fail=True)  # 不该走到 LLM
    _sent, why = asyncio.run(pc.proactive_speak(bot=FakeBot(), event=_event(), lines=["a：1"]))
    assert why == "context"
    assert "prompt" not in captured


def test_proactive_speak_llm_not_ready(monkeypatch):
    _patch_llm(monkeypatch, ready=False)
    _sent, why = asyncio.run(
        pc.proactive_speak(FakeBot(), _event(), ["a：1", "b：2", "c：3"])
    )
    assert why == "llm"


def test_proactive_speak_send_failure(monkeypatch):
    _patch_llm(monkeypatch, reply="来一句")
    _sent, why = asyncio.run(
        pc.proactive_speak(FakeBot(send_ok=False), _event(), ["a：1", "b：2", "c：3"])
    )
    assert why == "send"


def test_admin_plugin_names_cover_all():
    from src.plugins.admin import PLUGIN_NAMES
    from src.shared.storage import PLUGINS

    # 中文名表必须覆盖全部插件，/bot status 才能全用中文展示
    assert set(PLUGIN_NAMES) == set(PLUGINS)
