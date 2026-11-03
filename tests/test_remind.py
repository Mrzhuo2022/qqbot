"""定时提醒插件测试：时长解析、存取、到点发送/重试、列表格式化。"""

import asyncio
import datetime as dt

from src.plugins import remind as rm

# ── parse_duration ────────────────────────────────────────

def test_parse_duration_bare_number_is_minutes():
    assert rm.parse_duration("30") == 1800
    assert rm.parse_duration("1") == 60


def test_parse_duration_units():
    assert rm.parse_duration("30s") == 30
    assert rm.parse_duration("30秒") == 30
    assert rm.parse_duration("10min") == 600
    assert rm.parse_duration("10分钟") == 600
    assert rm.parse_duration("10m") == 600
    assert rm.parse_duration("1.5h") == 5400
    assert rm.parse_duration("2小时") == 7200
    assert rm.parse_duration("1d") == 86400
    assert rm.parse_duration("1天") == 86400
    assert rm.parse_duration("30MIN") == 1800  # 大小写不敏感


def test_parse_duration_composed():
    assert rm.parse_duration("1小时30分") == 3600 + 1800
    assert rm.parse_duration("2m30s") == 150


def test_parse_duration_rejects():
    assert rm.parse_duration("") is None
    assert rm.parse_duration("abc") is None
    assert rm.parse_duration("30minx") is None  # 尾部杂字
    assert rm.parse_duration("x30min") is None
    assert rm.parse_duration("0") is None  # 零分钟
    assert rm.parse_duration("8d") is None  # 超 7 天
    assert rm.parse_duration("-5") is None


# ── 存储 + 到点发送 ────────────────────────────────────────

def _with_store(monkeypatch, initial=None) -> dict:
    data = {"reminders": initial or []}
    monkeypatch.setattr(rm, "get_setting", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(rm, "set_setting", lambda k, v: data.__setitem__(k, v))
    return data


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_api(self, api, **params):
        self.calls.append((api, params))
        return {"message_id": 1}


def test_fire_group_sends_at_and_removes(monkeypatch):
    data = _with_store(monkeypatch, [
        {"id": 7, "uid": 123, "gid": 10000, "text": "拿快递", "due": "x", "retries": 0},
    ])
    bot = FakeBot()
    monkeypatch.setattr(rm, "get_bot", lambda: bot)
    asyncio.run(rm._fire(7))
    api, params = bot.calls[0]
    assert api == "send_group_msg"
    assert params["group_id"] == 10000
    # 消息含 @段 + 提醒文本（str(Message) 是 CQ 码形式）
    assert "CQ:at" in str(params["message"]) and "拿快递" in str(params["message"])
    assert data["reminders"] == []


def test_fire_private_sends_private_msg(monkeypatch):
    data = _with_store(monkeypatch, [
        {"id": 8, "uid": 123, "gid": None, "text": "喝水", "due": "x", "retries": 0},
    ])
    bot = FakeBot()
    monkeypatch.setattr(rm, "get_bot", lambda: bot)
    asyncio.run(rm._fire(8))
    api, params = bot.calls[0]
    assert api == "send_private_msg"
    assert params["user_id"] == 123
    assert data["reminders"] == []


def test_fire_missing_record_is_noop(monkeypatch):
    _with_store(monkeypatch, [])
    bot = FakeBot()
    monkeypatch.setattr(rm, "get_bot", lambda: bot)
    asyncio.run(rm._fire(99))
    assert bot.calls == []


def test_fire_failure_persists_retry(monkeypatch):
    data = _with_store(monkeypatch, [
        {"id": 7, "uid": 1, "gid": None, "text": "x", "due": "x", "retries": 0},
    ])
    rescheduled = []

    def boom():
        raise RuntimeError("no bot connected")

    monkeypatch.setattr(rm, "get_bot", boom)
    monkeypatch.setattr(
        rm.scheduler, "add_job", lambda *a, **k: rescheduled.append(k.get("args"))
    )
    asyncio.run(rm._fire(7))
    assert data["reminders"][0]["retries"] == 1
    assert rescheduled == [[7]]


def test_fire_gives_up_after_max_retries(monkeypatch):
    data = _with_store(monkeypatch, [
        {"id": 7, "uid": 1, "gid": None, "text": "x", "due": "x",
         "retries": rm.MAX_SEND_RETRIES - 1},
    ])

    def boom():
        raise RuntimeError("no bot connected")

    monkeypatch.setattr(rm, "get_bot", boom)
    monkeypatch.setattr(rm.scheduler, "add_job", lambda *a, **k: None)
    asyncio.run(rm._fire(7))
    assert data["reminders"] == []


# ── 列表格式化 ────────────────────────────────────────────

def test_format_list_only_mine_and_sorted():
    now = dt.datetime.now()
    reminders = [
        {"id": 3, "uid": 1, "gid": 100, "text": "晚的",
         "due": (now + dt.timedelta(hours=2)).isoformat(timespec="seconds")},
        {"id": 2, "uid": 1, "gid": None, "text": "早的",
         "due": (now + dt.timedelta(hours=1)).isoformat(timespec="seconds")},
        {"id": 9, "uid": 2, "gid": 100, "text": "别人的",
         "due": (now + dt.timedelta(minutes=1)).isoformat(timespec="seconds")},
    ]
    text = rm._format_list(reminders, 1)
    assert "#2" in text and "#3" in text and "#9" not in text
    assert text.index("#2") < text.index("#3")


def test_format_list_empty():
    assert "没有待提醒" in rm._format_list([], 1)


# ── 账本并发：发送 await 期间的取消不被旧账本复活 ──────────

def test_cancel_wins_over_concurrent_fire(monkeypatch):
    data = _with_store(monkeypatch, [
        {"id": 7, "uid": 1, "gid": None, "text": "拿快递",
         "due": dt.datetime.now().isoformat(timespec="seconds"), "retries": 0},
    ])
    release = asyncio.Event()

    class StuckBot:
        async def call_api(self, api, **params):
            await release.wait()  # 卡在发送，模拟 await 让出事件循环
            return {}

    monkeypatch.setattr(rm, "get_bot", lambda: StuckBot())
    monkeypatch.setattr(rm.scheduler, "add_job", lambda *a, **k: None)

    async def cancel_during_send():
        # 修复前：cancel 无锁插队改账本，随后被 fire 的旧账本覆盖复活；
        # 修复后：排队等账本锁，写进的是 fire 保存后的新账本
        async with rm._remind_lock:
            rm.set_setting(
                rm.STORE_KEY,
                [r for r in rm.get_setting(rm.STORE_KEY, []) if r["id"] != 7],
            )

    async def run():
        fire = asyncio.create_task(rm._fire(7))
        await asyncio.sleep(0.05)  # _fire 已持账本锁并卡在发送 await
        cancel = asyncio.create_task(cancel_during_send())
        await asyncio.sleep(0.05)  # cancel 已挂起在账本锁上
        release.set()              # 放行发送 → fire 保存并释放锁 → cancel 接着跑
        await asyncio.gather(fire, cancel)

    asyncio.run(run())
    assert data.get(rm.STORE_KEY) == []  # 发完即删，没有被 fire 的旧账本复活
