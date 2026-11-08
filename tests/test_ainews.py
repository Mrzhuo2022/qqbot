"""AI 热点插件的报文解析、格式化与日报推送测试（fixture 来自 aihot.news 真实接口结构）。"""

import asyncio
import datetime as dt

import src.plugins.ainews as an
from src.plugins.ainews import _extract_daily, _extract_hot, _fmt_daily, _fmt_hot
from src.shared.config import plugin_config

# ── 真实接口形态的 fixture ─────────────────────────────────

HOT_PAYLOAD = {
    "schemaVersion": 1,
    "count": 3,
    "items": [
        {
            "rank": 1,
            "id": "xywgx35ocbe6mzl2pnajw8ruu",
            "title": "Anthropic 拟感恩节前 IPO，估值预期下调",
            "source": {"name": "Anthropic：Newsroom（网页）"},
            "links": {"aihot": "https://aihot.news/items/a", "original": "https://example.com/1"},
            "sourceCount": 3,
            "signalCount": 17,
            "participantCount": 20,
            "sourceNames": ["IT之家·人工智能", "X：Rohan Paul (@rohanpaul_ai)"],
            "latestAt": "2026-11-08T00:44:04.537Z",
        },
        {"rank": 2, "id": "b", "title": "OpenAI 110亿美元首笔融资到账", "source": {}, "links": {}},
        {"rank": 3, "id": "c", "title": "Claude 智能体记忆机制拆解", "source": {}, "links": {}},
    ],
}

REPORT = {
    "date": "2026-11-08",
    "generatedAt": "2026-11-08T00:00:31.968Z",
    "windowStart": "2026-11-07T00:00:00.000Z",
    "windowEnd": "2026-11-08T00:00:00.000Z",
    "lead": {"title": "Anthropic 冲刺 IPO", "leadParagraph": "这是今天的导语。"},
    "sections": [
        {
            "label": "模型与产品",
            "items": [
                {"title": "Claude 更新", "summary": "支持记忆组件。", "links": {}},
                {"title": "无摘要条目", "summary": "", "links": {}},
            ],
        },
        {"label": "空板块", "items": []},
    ],
    "flashes": [{"title": "快讯一"}, {"title": "快讯二"}],
    "attribution": {"name": "AIHOT", "url": "https://aihot.news"},
}

DAILY_PAYLOAD = {"schemaVersion": 1, "report": REPORT}


# ── 热点榜解析 ─────────────────────────────────────────────

def test_extract_hot_caps_at_10():
    payload = {"items": [{"title": f"热点{i}"} for i in range(30)]}
    assert len(_extract_hot(payload)) == 10


def test_extract_hot_skips_empty_titles():
    payload = {"items": [{"url": "https://x"}, {"title": "有效"}, {"title": "  "}]}
    assert _extract_hot(payload) == ["有效"]


def test_extract_hot_rejects_bad_payload():
    assert _extract_hot({"foo": 1}) is None
    assert _extract_hot({"items": []}) is None
    assert _extract_hot({"items": "不是列表"}) is None


# ── 日报解析 ───────────────────────────────────────────────

def test_extract_daily_valid():
    assert _extract_daily(DAILY_PAYLOAD) is REPORT


def test_extract_daily_rejects_bad_payload():
    assert _extract_daily({"foo": 1}) is None
    assert _extract_daily({"report": "不是对象"}) is None
    assert _extract_daily({"report": {"lead": {}}}) is None  # 没有 date


# ── 格式化 ─────────────────────────────────────────────────

def test_fmt_hot_numbered():
    now = dt.datetime(2026, 11, 8, 9, 5, tzinfo=an.CN_TZ)
    text = _fmt_hot(_extract_hot(HOT_PAYLOAD), now=now)
    assert "🔥 AI 热点 Top 3 · 11-08 09:05" in text
    assert "1. Anthropic 拟感恩节前 IPO，估值预期下调" in text
    assert "2. OpenAI 110亿美元首笔融资到账" in text
    # 原始 JSON 字段（sourceNames/links）不会带进输出
    assert "rohanpaul_ai" not in text


def test_fmt_hot_strips_cq_codes():
    payload = {"items": [{"title": "标题[CQ:at,qq=123]后续"}]}
    assert "[CQ:at" not in _fmt_hot(_extract_hot(payload))


def test_fmt_daily_full_report():
    text = _fmt_daily(REPORT)
    assert "📅 AI 日报 · 2026-11-08" in text
    assert "Anthropic 冲刺 IPO" in text
    assert "这是今天的导语。" in text
    assert "【模型与产品】" in text
    assert "1. Claude 更新：支持记忆组件。" in text
    assert "2. 无摘要条目" in text  # 无摘要不挂冒号
    assert "【空板块】" not in text  # 没有条目的板块整段略过
    assert "⚡ 快讯" in text
    assert "· 快讯一" in text
    assert "via aihot.news" not in text
    assert "generatedAt" not in text  # 元字段不进正文


def test_fmt_daily_truncates_summary():
    report = {
        "date": "2026-11-08",
        "lead": {},
        "sections": [{"label": "板块", "items": [{"title": "标题", "summary": "长" * 100}]}],
    }
    text = _fmt_daily(report)
    assert "长" * 80 + "…" in text
    assert "长" * 81 not in text


# ── build_*：抓取失败/坏报文返回 None ──────────────────────

def test_build_hot_none_on_failure(monkeypatch):
    async def fail(path):
        return None

    async def bad(path):
        return {"foo": 1}

    monkeypatch.setattr(an, "_fetch_json", fail)
    assert asyncio.run(an.build_hot()) is None
    monkeypatch.setattr(an, "_fetch_json", bad)
    assert asyncio.run(an.build_hot()) is None


def test_build_daily_returns_date_and_text(monkeypatch):
    async def ok(path):
        return DAILY_PAYLOAD

    monkeypatch.setattr(an, "_fetch_json", ok)
    an._daily_cache._data.clear()
    date, text = asyncio.run(an.build_daily())
    assert date == "2026-11-08"
    assert "AI 日报" in text
    # 第二次直接命中缓存，不再发请求
    async def boom(path):
        raise AssertionError("不应再发请求")

    monkeypatch.setattr(an, "_fetch_json", boom)
    assert asyncio.run(an.build_daily()) == (date, text)


# ── push_daily：按日报日期去重，只在发出去后记标记 ─────────

def test_push_daily_dedup_by_report_date(monkeypatch):
    settings: dict = {"ainews:last_push": "2026-11-08"}
    monkeypatch.setattr(an, "get_setting", lambda k, default=None: settings.get(k, default))
    monkeypatch.setattr(an, "set_setting", lambda k, v: settings.__setitem__(k, v))
    monkeypatch.setattr(plugin_config, "ainews_groups", [111])
    monkeypatch.setattr(plugin_config, "group_plugin_off", {})

    async def fake_daily():
        return ("2026-11-08", "📅 日报")

    monkeypatch.setattr(an, "build_daily", fake_daily)

    class FakeBot:
        async def call_api(self, api, **kw):
            return {}

    monkeypatch.setattr(an, "get_bot", lambda: FakeBot())
    assert "已经推送过" in asyncio.run(an._push_daily())[1]  # 去重命中，不发

    settings["ainews:last_push"] = "2026-11-07"  # 昨天推过，今天还没推
    ok, msg = asyncio.run(an._push_daily())
    assert ok == 1 and "1/1" in msg
    assert settings["ainews:last_push"] == "2026-11-08"  # 标记记的是日报日期


def test_push_daily_marks_pushed_only_when_delivered(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(an, "set_setting", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(an, "get_setting", lambda k, default=None: saved.get(k, default))
    monkeypatch.setattr(plugin_config, "ainews_groups", [111, 222])
    monkeypatch.setattr(plugin_config, "group_plugin_off", {})

    async def fake_daily():
        return ("2026-11-08", "📅 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(an, "build_daily", fake_daily)
    monkeypatch.setattr(asyncio, "sleep", nosleep)

    class FakeBot:
        def __init__(self, ok: bool):
            self.ok = ok

        async def call_api(self, api, **kw):
            if not self.ok:
                raise RuntimeError("bot offline")

    monkeypatch.setattr(an, "get_bot", lambda: FakeBot(ok=False))
    result = asyncio.run(an.push_daily(force=True))
    assert "0/2" in result
    assert "ainews:last_push" not in saved

    monkeypatch.setattr(an, "get_bot", lambda: FakeBot(ok=True))
    result = asyncio.run(an.push_daily(force=True))
    assert "2/2" in result
    assert saved.get("ainews:last_push") == "2026-11-08"


def test_push_daily_respects_group_switch(monkeypatch):
    monkeypatch.setattr(plugin_config, "ainews_groups", [111, 222, 333])
    # 群 222 在 group_plugin_off 里关掉了 ainews
    monkeypatch.setattr(plugin_config, "group_plugin_off", {222: ["ainews"]})
    monkeypatch.setattr(an, "set_setting", lambda k, v: None)
    monkeypatch.setattr(an, "get_setting", lambda k, default=None: None)
    sent = []

    class FakeBot:
        async def call_api(self, api, **kw):
            sent.append(kw.get("group_id"))
            return {}

    async def fake_daily():
        return ("2026-11-08", "📅 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(an, "build_daily", fake_daily)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(an, "get_bot", lambda: FakeBot())
    result = asyncio.run(an.push_daily(force=True))
    assert sent == [111, 333]
    assert "2/2" in result  # 关掉的群不计入目标


def test_push_daily_no_targets(monkeypatch):
    monkeypatch.setattr(plugin_config, "ainews_groups", [])
    monkeypatch.setattr(plugin_config, "ainews_users", [])
    monkeypatch.setattr(plugin_config, "group_whitelist", [])

    async def boom(path):
        raise AssertionError("没有目标群就不该发请求")

    monkeypatch.setattr(an, "_fetch_json", boom)
    ok, msg = asyncio.run(an._push_daily(force=True))
    assert ok == -1 and "没有推送目标" in msg


def test_push_daily_sends_to_users(monkeypatch):
    monkeypatch.setattr(plugin_config, "ainews_groups", [])
    monkeypatch.setattr(plugin_config, "group_whitelist", [])
    monkeypatch.setattr(plugin_config, "ainews_users", [999, 888])
    monkeypatch.setattr(an, "set_setting", lambda k, v: None)
    monkeypatch.setattr(an, "get_setting", lambda k, default=None: None)
    sent = []

    class FakeBot:
        async def call_api(self, api, **kw):
            sent.append((api, kw.get("user_id")))

    async def fake_daily():
        return ("2026-11-08", "📅 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(an, "build_daily", fake_daily)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(an, "get_bot", lambda: FakeBot())
    result = asyncio.run(an.push_daily(force=True))
    assert sent == [
        ("send_private_msg", 999),
        ("send_private_msg", 888),
    ]
    assert "2/2" in result


def test_push_daily_mixed_group_and_user_targets(monkeypatch):
    monkeypatch.setattr(plugin_config, "ainews_groups", [111])
    monkeypatch.setattr(plugin_config, "group_whitelist", [])
    monkeypatch.setattr(plugin_config, "ainews_users", [999])
    monkeypatch.setattr(an, "set_setting", lambda k, v: None)
    monkeypatch.setattr(an, "get_setting", lambda k, default=None: None)
    sent = []

    class FakeBot:
        async def call_api(self, api, **kw):
            sent.append(api)

    async def fake_daily():
        return ("2026-11-08", "📅 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(an, "build_daily", fake_daily)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(an, "get_bot", lambda: FakeBot())
    result = asyncio.run(an.push_daily(force=True))
    assert sent == ["send_group_msg", "send_private_msg"]
    assert "2/2" in result


# ── 定时推送失败自动重试 ──────────────────────────────────

def _patch_retry_env(monkeypatch, push_result):
    scheduled = []

    async def fake_push(force=False):
        return push_result

    monkeypatch.setattr(an, "_push_daily", fake_push)
    monkeypatch.setattr(
        an.scheduler, "add_job",
        lambda *a, **k: scheduled.append((k.get("args"), k.get("id"))),
    )
    return scheduled


def test_auto_push_retries_when_all_failed(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (0, "日报源失败了"))
    asyncio.run(an._auto_push())
    assert scheduled == [([1], "ainews_retry")]


def test_auto_push_stops_at_retry_cap(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (0, "日报源失败了"))
    asyncio.run(an._auto_push(retries=an.PUSH_RETRIES))
    assert scheduled == []


def test_auto_push_no_retry_when_pushed_or_no_attempt(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (-1, "今天的日报已经推送过了"))
    asyncio.run(an._auto_push())
    assert scheduled == []

    scheduled = _patch_retry_env(monkeypatch, (2, "已推送到 2/2 个群"))
    asyncio.run(an._auto_push())
    assert scheduled == []
