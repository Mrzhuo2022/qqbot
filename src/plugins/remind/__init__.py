"""定时提醒插件：/remind 30min 拿快递 —— 到点在原地 @ 你。

- 时长写法：纯数字 = 分钟；30s/30秒、10m/10分钟、2h/2小时、1d/1天，
  可拼接（1小时30分）；上限 7 天
- /remind list 我的提醒；/remind cancel <编号> 取消（只能动自己的）
- 提醒存 data/settings.json，重启不丢：到点未响的重排，停机超过 1 小时的
  过期提醒不再补发；发送失败（bot 掉线）自动每分钟重试，最多 3 次
- 提醒文本过净化器（剥 CQ 码）——用户输入不能借 bot 之口发任意消息段
- 群里发的到点回群里 @，私聊发的到点私信
"""

import asyncio
import datetime as dt
import logging
import re

from nonebot import get_bot, on_command, require
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from src.shared.storage import get_setting, set_setting
from src.shared.utils import group_plugin_allowed, sanitize_bot_output

logger = logging.getLogger("remind")

# 每人同时持有的提醒上限（防刷调度器）
MAX_PER_USER = 5
MIN_SECONDS = 1
MAX_SECONDS = 7 * 86400
MAX_TEXT = 60
MAX_SEND_RETRIES = 3
# 停机期间错过的提醒超过这个时长就不补发（重启后狂补旧账没有意义）
STALE_AFTER = dt.timedelta(hours=1)

STORE_KEY = "reminders"

# 账本读改写互斥：_fire 在发送 await 期间事件循环会跑别的协程
# （用户 cancel、另一条提醒到点），裸读改写会互相覆盖（取消的提醒被复活）
_remind_lock = asyncio.Lock()

# 多字符单位排前面，避免「分钟」被「分」抢先匹配后剩下个「钟」字
_DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(sec|s|秒|min|分钟|分|m|hour|小时|h|day|天|d)", re.I
)
UNIT_SECONDS = {
    "sec": 1, "s": 1, "秒": 1,
    "min": 60, "分钟": 60, "m": 60, "分": 60,
    "hour": 3600, "小时": 3600, "h": 3600,
    "day": 86400, "天": 86400, "d": 86400,
}

USAGE = (
    "用法：/remind <时长> <事情>，如 /remind 30min 拿快递\n"
    "时长：纯数字=分钟，或 30s/10分钟/2小时/1天（可拼 1小时30分），最长 7 天\n"
    "/remind list 看我的提醒，/remind cancel 编号 取消"
)


def parse_duration(spec: str) -> int | None:
    """时长字符串 → 秒；纯数字按分钟。解析不出或超出范围返回 None。"""
    spec = spec.strip().lower()
    if not spec:
        return None
    if spec.isdigit():
        secs = int(spec) * 60
    else:
        total, pos = 0.0, 0
        for m in _DUR_RE.finditer(spec):
            if m.start() != pos:  # 中间夹着没认出来的杂字，整体不认
                return None
            total += float(m.group(1)) * UNIT_SECONDS[m.group(2).lower()]
            pos = m.end()
        if pos != len(spec) or pos == 0:
            return None
        secs = int(total)
    return secs if MIN_SECONDS <= secs <= MAX_SECONDS else None


# ── 存储：settings.json 里的一个 list ───────────────────

def _load() -> list[dict]:
    data = get_setting(STORE_KEY, [])
    return [r for r in data if isinstance(r, dict) and "id" in r] if isinstance(data, list) else []


def _save(reminders: list[dict]) -> None:
    set_setting(STORE_KEY, reminders)


def _job_id(rid: int) -> str:
    return f"remind:{rid}"


async def _fire(rid: int) -> None:
    """到点发送；发送失败（bot 掉线等）每分钟重试，超过次数放弃。

    全程持账本锁：发送的 await 让出事件循环时，cancel/别的提醒可能正在改账本。
    """
    async with _remind_lock:
        reminders = _load()
        rec = next((r for r in reminders if r["id"] == rid), None)
        if rec is None:
            return
        sent = False
        try:
            bot = get_bot()
            if rec.get("gid"):
                msg = MessageSegment.at(rec["uid"]) + f" ⏰ 到点啦：{rec['text']}"
                await bot.call_api("send_group_msg", group_id=rec["gid"], message=msg)
            else:
                await bot.call_api(
                    "send_private_msg", user_id=rec["uid"],
                    message=f"⏰ 到点啦：{rec['text']}",
                )
            sent = True
        except Exception:
            logger.exception("提醒 %s 发送失败", rid)

        if sent:
            _save([r for r in reminders if r["id"] != rid])
            return
        rec["retries"] = int(rec.get("retries", 0)) + 1
        if rec["retries"] >= MAX_SEND_RETRIES:
            logger.error("提醒 %s 重试 %d 次仍失败，放弃", rid, MAX_SEND_RETRIES)
            _save([r for r in reminders if r["id"] != rid])
            return
        _save(reminders)
    scheduler.add_job(
        _fire, "date",
        run_date=dt.datetime.now() + dt.timedelta(seconds=60),
        args=[rid], id=_job_id(rid), replace_existing=True,
    )


def _reschedule_all() -> None:
    """启动时把没响的提醒重新挂上调度器；停机太久的过期提醒直接丢弃。"""
    reminders = _load()
    now = dt.datetime.now()
    alive = []
    for rec in reminders:
        try:
            due = dt.datetime.fromisoformat(str(rec.get("due", "")))
        except ValueError:
            continue
        if due < now - STALE_AFTER:
            continue
        alive.append(rec)
        scheduler.add_job(
            _fire, "date", run_date=max(due, now + dt.timedelta(seconds=5)),
            args=[rec["id"]], id=_job_id(rec["id"]), replace_existing=True,
        )
    if len(alive) != len(reminders):
        logger.info("丢弃 %d 条过期/损坏的提醒", len(reminders) - len(alive))
        _save(alive)


_reschedule_all()


# 优先级 4：抢在 chat(5) 之前，避免 "/remind" 被当成聊天内容
remind_cmd = on_command(
    "remind", aliases={"提醒"}, rule=group_plugin_allowed("remind"), priority=4, block=True
)


def _format_list(reminders: list[dict], uid: int) -> str:
    mine = sorted((r for r in reminders if r["uid"] == uid), key=lambda r: r["due"])
    if not mine:
        return "你眼下没有待提醒的事～"
    today = dt.date.today()
    lines = []
    for r in mine:
        try:
            due = dt.datetime.fromisoformat(r["due"])
            when = due.strftime("%H:%M") if due.date() == today else due.strftime("%m-%d %H:%M")
        except ValueError:
            when = "?"
        where = "群" if r.get("gid") else "私信"
        lines.append(f"#{r['id']} {when}（{where}）{r['text']}")
    return "你的提醒：\n" + "\n".join(lines)


@remind_cmd.handle()
async def handle_remind(event: MessageEvent, args: Message = CommandArg()):
    raw = args.extract_plain_text().strip()
    tokens = raw.split(maxsplit=1)
    head = tokens[0].lower() if tokens else ""
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if head in ("list", "ls", "列表"):
        await remind_cmd.finish(_format_list(_load(), event.user_id))

    if head in ("cancel", "del", "rm", "取消"):
        if not rest.isdigit():
            await remind_cmd.finish("要取消哪条？/remind cancel 编号（list 里能看到）")
        rid = int(rest)
        async with _remind_lock:
            reminders = _load()
            rec = next(
                (r for r in reminders if r["id"] == rid and r["uid"] == event.user_id),
                None,
            )
            if rec is None:
                await remind_cmd.finish("没找到你的这条提醒（只能取消自己的）")
            _save([r for r in reminders if r["id"] != rid])
        try:
            scheduler.remove_job(_job_id(rid))
        except Exception:
            pass  # 任务可能已执行完
        await remind_cmd.finish(f"已取消 #{rid}：{rec['text']} ✓")

    if not raw:
        await remind_cmd.finish(USAGE)

    spec, _, text = raw.partition(" ")
    secs = parse_duration(spec)
    if secs is None or not text:
        await remind_cmd.finish(USAGE)
    text = sanitize_bot_output(text)[:MAX_TEXT] or "（空提醒）"

    async with _remind_lock:
        reminders = _load()
        if sum(1 for r in reminders if r["uid"] == event.user_id) >= MAX_PER_USER:
            await remind_cmd.finish(
                f"你已经排了 {MAX_PER_USER} 条提醒，先 /remind cancel 掉几个再来"
            )

        rid = int(get_setting("remind:next_id", 1))
        set_setting("remind:next_id", rid + 1)
        due = dt.datetime.now() + dt.timedelta(seconds=secs)
        rec = {
            "id": rid,
            "uid": event.user_id,
            "gid": event.group_id if isinstance(event, GroupMessageEvent) else None,
            "text": text,
            "due": due.isoformat(timespec="seconds"),
            "retries": 0,
        }
        reminders.append(rec)
        _save(reminders)
    scheduler.add_job(
        _fire, "date", run_date=due,
        args=[rid], id=_job_id(rid), replace_existing=True,
    )
    await remind_cmd.finish(
        f"好，{due.strftime('%m-%d %H:%M')} 提醒你：{text}"
        + ("（到点在群里 @ 你）" if rec["gid"] else "（到点私信你）")
    )
