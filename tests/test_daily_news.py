"""定时新闻插件的时间解析、报文解析与早安导语测试。"""

import asyncio
import datetime as dt

import src.plugins.daily_news as dn
from src.plugins.daily_news import _extract_items
from src.shared.config import plugin_config
from src.shared.utils import parse_hhmm as _parse_hhmm


def test_parse_hhmm_valid():
    assert _parse_hhmm("08:00") == (8, 0)
    assert _parse_hhmm("23:59") == (23, 59)
    assert _parse_hhmm("8:05") == (8, 5)


def test_parse_hhmm_invalid():
    assert _parse_hhmm("24:00") is None
    assert _parse_hhmm("08:60") is None
    assert _parse_hhmm("八点") is None
    assert _parse_hhmm("") is None
    assert _parse_hhmm("0830") is None


def test_extract_items_v2_wrapped():
    payload = {"code": 200, "data": {"date": "2026-09-11", "news": ["新闻A", "新闻B"]}}
    date_str, items, _ = _extract_items(payload)
    assert date_str == "2026-09-11"
    assert items == ["新闻A", "新闻B"]


def test_extract_items_topurl_shape():
    payload = {
        "code": 200,
        "data": {
            "newsList": [
                {"title": "标题一", "url": "https://x/1", "category": "商业"},
                {"title": "标题二", "url": "https://x/2", "category": "科技"},
            ],
            "sentence": "每日一句：行胜于言。",
        },
    }
    _, items, quote = _extract_items(payload)
    assert items == ["标题一", "标题二"]
    assert "行胜于言" in quote


def test_extract_items_flat():
    _, items, quote = _extract_items({"news": [1, 2, 3]})
    assert items == ["1", "2", "3"]
    assert quote == ""


def test_extract_items_caps_at_20():
    payload = {"news": [f"n{i}" for i in range(30)]}
    assert len(_extract_items(payload)[1]) == 20


def test_extract_items_skips_empty_dict_titles():
    payload = {"data": {"newsList": [{"url": "https://x"}, {"title": "有效"}]}}
    assert _extract_items(payload)[1] == ["有效"]


def test_cn_weekday():
    # 2026-11-04 是周三
    assert dn._cn_weekday(dt.date(2026, 11, 4)) == "周三"
    assert dn._cn_weekday(dt.date(2026, 11, 1)) == "周日"


def test_gen_intro_injects_date_and_weekday(monkeypatch):
    """导语 system 要带日期和星期，LLM 才说得出「周三早上好」。"""
    monkeypatch.setattr(plugin_config, "daily_news_intro", True)
    monkeypatch.setattr(dn.llm, "llm_ready", lambda: True)
    captured: dict = {}

    async def fake_chat(system, prompt, **kw):
        captured["system"] = system
        captured["prompt"] = prompt
        return "早安，新的一天"

    monkeypatch.setattr(dn.llm, "llm_chat", fake_chat)
    intro = asyncio.run(dn._gen_intro(["新闻A"]))

    assert intro == "早安，新的一天"
    assert dn._today() in captured["system"]
    assert dn._cn_weekday(dt.date.today()) in captured["system"]
    assert "<untrusted_data>" in captured["prompt"]


def test_gen_intro_off_or_no_llm(monkeypatch):
    monkeypatch.setattr(plugin_config, "daily_news_intro", False)
    assert asyncio.run(dn._gen_intro(["新闻A"])) == ""
    monkeypatch.setattr(plugin_config, "daily_news_intro", True)
    monkeypatch.setattr(dn.llm, "llm_ready", lambda: False)
    assert asyncio.run(dn._gen_intro(["新闻A"])) == ""


def test_extract_items_rejects_bad_payload():
    assert _extract_items({"foo": 1}) is None
    assert _extract_items({"news": []}) is None
    assert _extract_items({"data": {"news": "不是列表"}}) is None


# ── push_news：只在至少发出去一个群时才记「今日已推」标记 ──


def test_push_news_marks_pushed_only_when_delivered(monkeypatch):
    from src.plugins import daily_news as dn

    monkeypatch.setattr(plugin_config, "daily_news_groups", [111, 222])
    saved: dict = {}
    monkeypatch.setattr(dn, "set_setting", lambda k, v: saved.__setitem__(k, v))

    async def fake_digest():
        return "📰 digest"

    async def nosleep(_):
        return None

    monkeypatch.setattr(dn, "build_digest", fake_digest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)

    class FakeBot:
        def __init__(self, ok: bool):
            self.ok = ok

        async def call_api(self, api: str, **kw):
            if not self.ok:
                raise RuntimeError("bot offline")

    monkeypatch.setattr(dn, "get_bot", lambda: FakeBot(ok=False))
    result = asyncio.run(dn.push_news(force=True))
    assert "0/2" in result
    assert "news:last_push" not in saved

    monkeypatch.setattr(dn, "get_bot", lambda: FakeBot(ok=True))
    result = asyncio.run(dn.push_news(force=True))
    assert "2/2" in result
    assert saved.get("news:last_push")


# ── 定时推送失败自动重试 ──────────────────────────────────

def _patch_retry_env(monkeypatch, push_result):
    from src.plugins import daily_news as dn

    scheduled = []

    async def fake_push(force=False):
        return push_result

    monkeypatch.setattr(dn, "_push_news", fake_push)
    monkeypatch.setattr(
        dn.scheduler, "add_job",
        lambda *a, **k: scheduled.append((k.get("args"), k.get("id"))),
    )
    return dn, scheduled


def test_auto_push_retries_when_all_failed(monkeypatch):
    dn, scheduled = _patch_retry_env(monkeypatch, (0, "新闻源全都失败了"))
    asyncio.run(dn._auto_push())
    assert scheduled == [([1], "daily_news_retry")]


def test_auto_push_stops_at_retry_cap(monkeypatch):
    dn, scheduled = _patch_retry_env(monkeypatch, (0, "新闻源全都失败了"))
    asyncio.run(dn._auto_push(retries=dn.PUSH_RETRIES))
    assert scheduled == []


def test_auto_push_no_retry_when_pushed_or_no_attempt(monkeypatch):
    dn, scheduled = _patch_retry_env(monkeypatch, (-1, "今天已经推送过了"))
    asyncio.run(dn._auto_push())
    assert scheduled == []

    dn, scheduled = _patch_retry_env(monkeypatch, (2, "已推送到 2/2 个群"))
    asyncio.run(dn._auto_push())
    assert scheduled == []


def test_push_news_respects_group_switch(monkeypatch):
    from src.plugins import daily_news as dn

    monkeypatch.setattr(plugin_config, "daily_news_groups", [111, 222, 333])
    # 群 222 在 group_plugin_off 里关掉了 news
    monkeypatch.setattr(plugin_config, "group_plugin_off", {222: ["news"]})
    monkeypatch.setattr(dn, "set_setting", lambda k, v: None)
    sent = []

    class FakeBot:
        async def call_api(self, api, **kw):
            sent.append(kw.get("group_id"))
            return {}

    async def fake_digest():
        return "📰 digest"

    async def nosleep(_):
        return None

    monkeypatch.setattr(dn, "build_digest", fake_digest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(dn, "get_bot", lambda: FakeBot())
    result = asyncio.run(dn.push_news(force=True))
    assert sent == [111, 333]
    assert "2/2" in result  # 关掉的群不计入目标
