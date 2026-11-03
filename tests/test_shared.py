"""限流器、TTL 缓存、JSON 持久化的单元测试。"""

from src.shared.cache import TTLCache
from src.shared.rate_limit import RateLimiter
from src.shared.storage import JsonStore


def test_rate_limiter_allows_within_limit():
    rl = RateLimiter(max_calls=2, per_seconds=60)
    assert rl.check("g:1") is True
    assert rl.check("g:1") is True
    assert rl.check("g:1") is False


def test_rate_limiter_keys_are_independent():
    rl = RateLimiter(max_calls=1, per_seconds=60)
    assert rl.check("g:1") is True
    assert rl.check("g:2") is True
    assert rl.check("g:1") is False


def test_ttl_cache_hit_and_miss():
    cache = TTLCache(maxsize=8, ttl=60)
    assert cache.get("url") is None
    cache.set("url", "value")
    assert cache.get("url") == "value"


def test_ttl_cache_expiry():
    cache = TTLCache(maxsize=8, ttl=0)
    cache.set("url", "value")
    assert cache.get("url") is None


def test_ttl_cache_evicts_oldest():
    cache = TTLCache(maxsize=2, ttl=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_json_store_roundtrip(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    store.set("k", {"a": 1, "中文": "值"})
    reloaded = JsonStore(tmp_path / "state.json")
    assert reloaded.get("k") == {"a": 1, "中文": "值"}


def test_json_store_remove(tmp_path):
    path = tmp_path / "state.json"
    store = JsonStore(path)
    store.set("k", 1)
    store.remove("k")
    assert JsonStore(path).get("k") is None


def test_group_plugin_allowed_rule(monkeypatch):
    """命令类插件共用的「白名单+群级开关」规则：私聊放行，群聊按配置过滤。"""
    import asyncio

    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
    from nonebot.adapters.onebot.v11.event import Sender

    from src.shared import utils
    from src.shared.config import plugin_config

    def group(gid):
        return GroupMessageEvent(
            time=0, self_id="1", post_type="message", sub_type="normal",
            message_type="group", user_id=2, group_id=gid, message=Message("/x"),
            raw_message="/x", font=0, message_id=1,
            sender=Sender(user_id=2, nickname="u"), to_me=False,
        )

    def private():
        return PrivateMessageEvent(
            time=0, self_id="1", post_type="message", sub_type="friend",
            message_type="private", user_id=2, message=Message("/x"),
            raw_message="/x", font=0, message_id=1,
            sender=Sender(user_id=2, nickname="u"), to_me=False,
        )

    monkeypatch.setattr(plugin_config, "group_whitelist", [10])
    monkeypatch.setattr(
        utils, "group_plugin_enabled", lambda e, p: p != "closed_plugin"
    )
    rule = utils.group_plugin_allowed("news")
    assert asyncio.run(rule(group(10)))  # 白名单内 + 插件开
    assert not asyncio.run(rule(group(20)))  # 白名单外
    assert not asyncio.run(utils.group_plugin_allowed("closed_plugin")(group(10)))  # 插件关
    assert asyncio.run(rule(private()))  # 私聊放行


def test_send_reply_sanitizes_outbound(monkeypatch):
    """出群统一收口：send_reply 里的文本一律剥 CQ 码（昵称/聊天原文是用户可控的）。"""
    import asyncio
    from types import SimpleNamespace

    from src.shared.utils import send_reply

    sent = []

    class FakeMatcher:
        async def send(self, msg):
            sent.append(str(msg))

    event = SimpleNamespace(message_id=9)
    asyncio.run(
        send_reply(
            FakeMatcher(), event,
            "[CQ:at,qq=all] 注意啦\n来自 [CQ:image,file=x] 的消息", 100,
        )
    )
    # 首段带引用段（真实消息段，非文本），正文里的 CQ 码全部被剥掉
    assert sent == ["[CQ:reply,id=9]注意啦\n来自  的消息"]
    assert "[CQ:at" not in "".join(sent) and "[CQ:image" not in "".join(sent)
