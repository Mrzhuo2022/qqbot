"""HN 日报插件的报文读取、格式化与推送测试（fixture 来自 hn-daily 真实 payload 结构）。"""

import asyncio
import datetime as dt
import json

import src.plugins.hndaily as hd
from src.plugins.hndaily import _fmt_report, _load_report
from src.shared.config import plugin_config

# ── 真实 payload 形态的 fixture（hn-daily report.build_api_payload 的输出）──

REPORT = {
    "date": "2026-11-11",
    "generated_at": "2026-11-11T08:20:00+08:00",
    "theme": "AI 基础设施与开发者工具的一天",
    "summary": ["推理成本继续下探", "本地优先架构回潮"],
    "persona": "🌙 由灰之魔女伊蕾娜为您整理",
    "items": [
        {
            "id": 49076057,
            "title": "Rust 重写核心服务后，延迟降了一半",
            "title_original": "We rewrote our core service in Rust",
            "url": "https://example.com/rust",
            "hn_link": "https://news.ycombinator.com/item?id=49076057",
            "score": 924,
            "comments": 1340,
            "points": ["要点一", "要点二", "要点三"],
            "comment": "工程复盘写得扎实，值得精读。",
        },
        {
            "id": 49076001,
            "title": "Ask HN：你们怎么管理个人知识库？",
            "title_original": "Ask HN: How do you manage your PKM?",
            "url": None,
            "hn_link": "https://news.ycombinator.com/item?id=49076001",
            "score": 210,
            "comments": 87,
            "points": [],
            "comment": "",
        },
    ],
}


def _write_report(tmp_path, payload=REPORT):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


# ── 报文读取 ──────────────────────────────────────────────

def test_load_report_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_json", _write_report(tmp_path))
    assert _load_report() == REPORT


def test_load_report_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_json", str(tmp_path / "nope.json"))
    assert _load_report() is None


def test_load_report_bad_json(tmp_path, monkeypatch):
    p = tmp_path / "latest.json"
    p.write_text("{不是json", encoding="utf-8")
    monkeypatch.setattr(plugin_config, "hndaily_json", str(p))
    assert _load_report() is None


def test_load_report_rejects_no_date(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_json", _write_report(tmp_path, {"items": []}))
    assert _load_report() is None
    monkeypatch.setattr(plugin_config, "hndaily_json", _write_report(tmp_path, ["不是对象"]))
    assert _load_report() is None


# ── 格式化 ───────────────────────────────────────────────

def test_fmt_report_numbered():
    text = _fmt_report(REPORT)
    assert "📰 HN 每日科技精选 · 2026-11-11" in text
    assert "AI 基础设施与开发者工具的一天" in text
    # 标题 + 得分/评论一行，要点圆点，点评带 💬
    assert "1. Rust 重写核心服务后，延迟降了一半 ⭐924 💬1340" in text
    assert "· 要点一" in text
    assert "💬 工程复盘写得扎实，值得精读。" in text
    # 中文标题缺失时退回 title_original；无外链条目链接退回 hn_link
    assert "2. Ask HN：你们怎么管理个人知识库？ ⭐210 💬87" in text
    assert "https://example.com/rust" in text
    assert "https://news.ycombinator.com/item?id=49076001" in text
    assert "📋 今日总结" in text
    assert "🌙 由灰之魔女伊蕾娜为您整理" in text
    assert "generated_at" not in text  # 元字段不进正文


def test_fmt_report_strips_cq_codes():
    report = {**REPORT, "items": [{**REPORT["items"][0], "comment": "看[CQ:at,qq=all]这个"}]}
    assert "[CQ:at" not in _fmt_report(report)


def test_fmt_report_skips_bad_items():
    report = {
        "date": "2026-11-11",
        "items": [
            "不是字典",
            None,
            {"title": "  "},  # 空标题
            {"title": "有效条目", "url": "", "hn_link": "", "score": 1, "comments": 0},
            {"title_original": "Only original", "score": 2, "comments": 0},
        ],
    }
    text = _fmt_report(report)
    assert "1. 有效条目" in text  # 编号连续，坏条目不占号
    assert "2. Only original" in text
    assert "不是字典" not in text
    # 两个条目都没有任何链接，不挂空链接行
    assert "https://" not in text


# ── build_latest：读不到返回 None ─────────────────────────

def test_build_latest_none_on_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_json", str(tmp_path / "nope.json"))
    assert hd.build_latest() is None


def test_build_latest_returns_date_and_text(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_json", _write_report(tmp_path))
    date, text = hd.build_latest()
    assert date == "2026-11-11"
    assert "HN 每日科技精选" in text


# ── push_daily：判新 + 按报告日期去重，只在发出去后记标记 ──

def _patch_targets(monkeypatch, groups, users=()):
    monkeypatch.setattr(plugin_config, "hndaily_groups", groups)
    monkeypatch.setattr(plugin_config, "group_whitelist", [])
    monkeypatch.setattr(plugin_config, "hndaily_users", list(users))
    monkeypatch.setattr(plugin_config, "group_plugin_off", {})


def test_push_daily_dedup_by_report_date(monkeypatch):
    settings: dict = {"hndaily:last_push": "2026-11-11"}
    monkeypatch.setattr(hd, "get_setting", lambda k, default=None: settings.get(k, default))
    monkeypatch.setattr(hd, "set_setting", lambda k, v: settings.__setitem__(k, v))
    _patch_targets(monkeypatch, [111])

    def fake_latest():
        return ("2026-11-11", "📰 HN 日报")

    monkeypatch.setattr(hd, "build_latest", fake_latest)

    class FakeBot:
        async def call_api(self, api, **kw):
            return {}

    monkeypatch.setattr(hd, "get_bot", lambda: FakeBot())
    assert "已经推送过" in asyncio.run(hd._push_daily())[1]  # 去重命中，不发

    settings["hndaily:last_push"] = "2026-11-10"  # 昨天推过，今天还没推
    ok, msg = asyncio.run(hd._push_daily())
    assert ok == 1 and "1/1" in msg
    assert settings["hndaily:last_push"] == "2026-11-11"  # 标记记的是报告日期


def test_push_daily_skips_stale_report(monkeypatch):
    monkeypatch.setattr(hd, "get_setting", lambda k, default=None: None)
    monkeypatch.setattr(hd, "set_setting", lambda k, v: None)
    _patch_targets(monkeypatch, [111])

    def stale():
        return ("2026-11-10", "📰 昨天的日报")

    monkeypatch.setattr(hd, "build_latest", stale)

    class Boom:
        async def call_api(self, api, **kw):
            raise AssertionError("过期报告不该发出去")

    monkeypatch.setattr(hd, "get_bot", lambda: Boom())
    ok, msg = asyncio.run(hd._push_daily())
    assert ok == 0 and "还不是今天的" in msg  # 可重试失败

    # force（管理员手动 push）允许推过期报告
    class FakeBot:
        async def call_api(self, api, **kw):
            return {}

    monkeypatch.setattr(hd, "get_bot", lambda: FakeBot())
    ok, msg = asyncio.run(hd._push_daily(force=True))
    assert ok == 1 and "1/1" in msg


def test_push_daily_marks_pushed_only_when_delivered(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(hd, "set_setting", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(hd, "get_setting", lambda k, default=None: saved.get(k, default))
    _patch_targets(monkeypatch, [111, 222])

    def fake_latest():
        return ("2026-11-11", "📰 HN 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(hd, "build_latest", fake_latest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)

    class FakeBot:
        def __init__(self, ok: bool):
            self.ok = ok

        async def call_api(self, api, **kw):
            if not self.ok:
                raise RuntimeError("bot offline")

    monkeypatch.setattr(hd, "get_bot", lambda: FakeBot(ok=False))
    result = asyncio.run(hd.push_daily(force=True))
    assert "0/2" in result
    assert "hndaily:last_push" not in saved

    monkeypatch.setattr(hd, "get_bot", lambda: FakeBot(ok=True))
    result = asyncio.run(hd.push_daily(force=True))
    assert "2/2" in result
    assert saved.get("hndaily:last_push") == "2026-11-11"


def test_push_daily_respects_group_switch(monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_groups", [111, 222, 333])
    # 群 222 在 group_plugin_off 里关掉了 hndaily
    monkeypatch.setattr(plugin_config, "group_plugin_off", {222: ["hndaily"]})
    monkeypatch.setattr(plugin_config, "group_whitelist", [])
    monkeypatch.setattr(plugin_config, "hndaily_users", [])
    monkeypatch.setattr(hd, "set_setting", lambda k, v: None)
    monkeypatch.setattr(hd, "get_setting", lambda k, default=None: None)
    sent = []

    class FakeBot:
        async def call_api(self, api, **kw):
            sent.append(kw.get("group_id"))
            return {}

    def fake_latest():
        return ("2026-11-11", "📰 HN 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(hd, "build_latest", fake_latest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(hd, "get_bot", lambda: FakeBot())
    result = asyncio.run(hd.push_daily(force=True))
    assert sent == [111, 333]
    assert "2/2" in result  # 关掉的群不计入目标


def test_push_daily_no_targets(monkeypatch):
    monkeypatch.setattr(plugin_config, "hndaily_groups", [])
    monkeypatch.setattr(plugin_config, "hndaily_users", [])
    monkeypatch.setattr(plugin_config, "group_whitelist", [])

    def boom():
        raise AssertionError("没有目标就不该读文件")

    monkeypatch.setattr(hd, "build_latest", boom)
    ok, msg = asyncio.run(hd._push_daily(force=True))
    assert ok == -1 and "没有推送目标" in msg


def test_push_daily_sends_to_users(monkeypatch):
    _patch_targets(monkeypatch, [], users=[999, 888])
    monkeypatch.setattr(hd, "set_setting", lambda k, v: None)
    monkeypatch.setattr(hd, "get_setting", lambda k, default=None: None)
    sent = []

    class FakeBot:
        async def call_api(self, api, **kw):
            sent.append((api, kw.get("user_id")))

    def fake_latest():
        return ("2026-11-11", "📰 HN 日报")

    async def nosleep(_):
        return None

    monkeypatch.setattr(hd, "build_latest", fake_latest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    monkeypatch.setattr(hd, "get_bot", lambda: FakeBot())
    result = asyncio.run(hd.push_daily(force=True))
    assert sent == [
        ("send_private_msg", 999),
        ("send_private_msg", 888),
    ]
    assert "2/2" in result


# ── 定时推送失败自动重试 ──────────────────────────────────

def _patch_retry_env(monkeypatch, push_result):
    scheduled = []

    async def fake_push(force=False):
        return push_result

    monkeypatch.setattr(hd, "_push_daily", fake_push)
    monkeypatch.setattr(
        hd.scheduler, "add_job",
        lambda *a, **k: scheduled.append((k.get("args"), k.get("id"))),
    )
    return scheduled


def test_auto_push_retries_when_all_failed(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (0, "HN 日报读不到"))
    asyncio.run(hd._auto_push())
    assert scheduled == [([1], "hndaily_retry")]


def test_auto_push_stops_at_retry_cap(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (0, "HN 日报读不到"))
    asyncio.run(hd._auto_push(retries=hd.PUSH_RETRIES))
    assert scheduled == []


def test_auto_push_no_retry_when_pushed_or_no_attempt(monkeypatch):
    scheduled = _patch_retry_env(monkeypatch, (-1, "今天的 HN 日报已经推送过了"))
    asyncio.run(hd._auto_push())
    assert scheduled == []

    scheduled = _patch_retry_env(monkeypatch, (2, "已推送到 2/2 个目标"))
    asyncio.run(hd._auto_push())
    assert scheduled == []


# ── 定时任务注册 ──────────────────────────────────────────

def test_register_job_parses_hhmm(monkeypatch):
    registered = {}

    def fake_add_job(fn, trigger, **kw):
        registered.update(kw)

    monkeypatch.setattr(hd.scheduler, "add_job", fake_add_job)
    monkeypatch.setattr(plugin_config, "hndaily_time", "08:30")
    hd._register_job()
    assert registered["id"] == "hndaily_daily"
    assert registered["hour"] == 8 and registered["minute"] == 30

    # 留空关闭定时；格式错误报错也不注册
    monkeypatch.setattr(plugin_config, "hndaily_time", "")
    registered.clear()
    hd._register_job()
    assert registered == {}

    monkeypatch.setattr(plugin_config, "hndaily_time", "25:00")
    hd._register_job()
    assert registered == {}


def test_today_cn_is_beijing_date():
    now = dt.datetime(2026, 11, 11, 20, 0, tzinfo=dt.timezone.utc)
    assert hd._today_cn() == hd._now_cn().date().isoformat()  # 自洽即可
    assert hd.CN_TZ.utcoffset(now) == dt.timedelta(hours=8)
