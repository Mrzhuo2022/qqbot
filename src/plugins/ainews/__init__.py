"""AI 热点插件：/ainews 看 aihot.news 实时热点 Top10，每天定时推送 AI 日报。

- ainews_time：日报推送时间 HH:MM，留空关闭定时（/ainews 命令仍可用）
- ainews_groups：推送目标群号，留空跟随 group_whitelist
- ainews_users：推送目标个人 QQ 号（私聊），和群号可同时配
- ainews_api：aihot.news 站点地址，默认官方，一般不用改
- /ainews：当前热点 Top10；/ainews 日报：手动看今日日报；/ainews push：管理员立即推送
- 群级开关 /bot off AI热点：某群退出定时推送，/ainews 命令在该群也不可用
- 日报每天 08:00（北京时间）由源站生成，推送时间别配得比它早
- 日报内容来自第三方接口，按不可信数据处理；每天只推一次（按日报日期去重，落盘重启不重复推）
"""

import asyncio
import datetime as dt
import logging

import httpx
from nonebot import get_bot, on_command, require
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from src.shared.cache import TTLCache
from src.shared.config import plugin_config
from src.shared.storage import get_setting, group_plugin_enabled_id, set_setting
from src.shared.utils import (
    UA_HEADERS,
    clip,
    group_plugin_allowed,
    is_superuser,
    parse_hhmm,
    sanitize_bot_output,
    send_notice,
    send_reply,
)

logger = logging.getLogger("ainews")

HOT_TOPICS_PATH = "/api/v1/hot-topics"
DAILY_PATH = "/api/v1/dailies/latest"
CN_TZ = dt.timezone(dt.timedelta(hours=8))

# 热点榜源站 5 分钟刷新一次，本地缓存 10 分钟去抖；日报一天一份，缓存 3 小时
_hot_cache = TTLCache(maxsize=4, ttl=10 * 60)
_daily_cache = TTLCache(maxsize=4, ttl=3 * 3600)
# 日报单条 summary 的截断长度
SUMMARY_CLIP = 80


def _now_cn() -> dt.datetime:
    return dt.datetime.now(CN_TZ)


def _today_cn() -> str:
    return _now_cn().date().isoformat()


async def _fetch_json(path: str) -> dict | None:
    base = plugin_config.ainews_api.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15, headers=UA_HEADERS) as client:
            resp = await client.get(base + path)
            data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("aihot.news 接口失败 %s: %s", path, e)
        return None


def _extract_hot(payload: dict) -> list[str] | None:
    """hot-topics 返回 {items: [{rank, title, ...}]}，只取前 10 条标题。"""
    raw = payload.get("items")
    if not isinstance(raw, list):
        return None
    titles: list[str] = []
    for x in raw:
        if isinstance(x, dict):
            text = str(x.get("title", "")).strip()
            if text:
                titles.append(text)
        if len(titles) >= 10:
            break
    return titles or None


def _extract_daily(payload: dict) -> dict | None:
    """dailies/latest 返回 {report: {date, lead, sections, flashes, ...}}。"""
    report = payload.get("report")
    if not isinstance(report, dict) or not str(report.get("date", "")).strip():
        return None
    return report


def _fmt_hot(items: list[str], now: dt.datetime | None = None) -> str:
    ts = (now or _now_cn()).strftime("%m-%d %H:%M")
    lines = [
        f"🔥 AI 热点 Top {len(items)} · {ts}",
        *(f"{i}. {t}" for i, t in enumerate(items, 1)),
    ]
    return clip(sanitize_bot_output("\n".join(lines)), plugin_config.reply_max_chars)


def _fmt_daily(report: dict) -> str:
    lines = [f"📅 AI 日报 · {report.get('date')}"]
    lead = report.get("lead") if isinstance(report.get("lead"), dict) else {}
    title = str(lead.get("title", "")).strip()
    para = str(lead.get("leadParagraph", "")).strip()
    if title:
        lines += ["", title]
    if para:
        lines += [para]
    for sec in report.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        label = str(sec.get("label", "")).strip()
        entries: list[str] = []
        for x in sec.get("items") or []:
            if not isinstance(x, dict):
                continue
            t = str(x.get("title", "")).strip()
            if not t:
                continue
            s = clip(str(x.get("summary", "")).strip(), SUMMARY_CLIP)
            entries.append(f"{len(entries) + 1}. {t}：{s}" if s else f"{len(entries) + 1}. {t}")
        if entries:
            lines += ["", f"【{label}】", *entries]
    flashes: list[str] = []
    for x in report.get("flashes") or []:
        if isinstance(x, dict):
            t = str(x.get("title", "")).strip()
            if t:
                flashes.append(f"· {t}")
    if flashes:
        lines += ["", "⚡ 快讯", *flashes]
    return clip(sanitize_bot_output("\n".join(lines)), plugin_config.reply_max_chars)


async def build_hot() -> str | None:
    cached = _hot_cache.get("hot")
    if cached is not None:
        return cached
    payload = await _fetch_json(HOT_TOPICS_PATH)
    if payload is None:
        return None
    items = _extract_hot(payload)
    if not items:
        return None
    text = _fmt_hot(items)
    _hot_cache.set("hot", text)
    return text


async def build_daily() -> tuple[str, str] | None:
    """返回 (日报日期, 格式化文本)；源失败返回 None。"""
    cached = _daily_cache.get("daily")
    if cached is not None:
        return cached
    payload = await _fetch_json(DAILY_PATH)
    if payload is None:
        return None
    report = _extract_daily(payload)
    if report is None:
        return None
    result = (str(report.get("date")), _fmt_daily(report))
    _daily_cache.set("daily", result)
    return result


async def _push_daily(force: bool = False) -> tuple[int, str]:
    """推送核心，返回 (成功数, 描述)；提前退出（已推过/无目标）成功数为 -1。"""
    group_targets = [
        g
        for g in (plugin_config.ainews_groups or plugin_config.group_whitelist)
        if group_plugin_enabled_id(g, "ainews")
    ]
    user_targets = plugin_config.ainews_users
    if not group_targets and not user_targets:
        return -1, "没有推送目标：请配置 ainews_groups/ainews_users 或 group_whitelist"
    daily = await build_daily()
    if daily is None:
        return 0, "日报源失败了，稍后再试"
    date, text = daily
    if not force and get_setting("ainews:last_push") == date:
        return -1, "今天的日报已经推送过了"
    try:
        bot = get_bot()
    except Exception:
        return 0, "没有已连接的 QQ，推送失败"
    ok = 0
    total = len(group_targets) + len(user_targets)
    for gid in group_targets:
        try:
            await bot.call_api("send_group_msg", group_id=gid, message=text)
            ok += 1
        except Exception:
            logger.exception("日报推送到群 %s 失败", gid)
        await asyncio.sleep(1)
    for uid in user_targets:
        try:
            await bot.call_api("send_private_msg", user_id=uid, message=text)
            ok += 1
        except Exception:
            logger.exception("日报推送给 QQ %s 失败", uid)
        await asyncio.sleep(1)
    if ok:
        # 一个目标都没发出去就不记标记，稍后还能 /ainews push 重试
        set_setting("ainews:last_push", date)
    return ok, f"已推送到 {ok}/{total} 个目标"


async def push_daily(force: bool = False) -> str:
    return (await _push_daily(force))[1]


# 定时推送全军覆没时（源挂了/bot 掉线）的自动重试：每 30 分钟一次，最多 3 次
PUSH_RETRIES = 3
RETRY_DELAY_MIN = 30


async def _auto_push(retries: int = 0) -> None:
    ok, msg = await _push_daily()
    logger.info("AI 日报推送：%s", msg)
    if ok == 0 and retries < PUSH_RETRIES:
        run_at = dt.datetime.now() + dt.timedelta(minutes=RETRY_DELAY_MIN)
        scheduler.add_job(
            _auto_push, "date", run_date=run_at, args=[retries + 1],
            id="ainews_retry", replace_existing=True,
        )
        logger.info("全部失败，%s 自动重试（第 %d/%d 次）",
                    run_at.strftime("%H:%M"), retries + 1, PUSH_RETRIES)


def _register_job() -> None:
    parsed = parse_hhmm(plugin_config.ainews_time)
    if parsed is None:
        if plugin_config.ainews_time.strip():
            logger.error("ainews_time 应为 HH:MM 格式，定时推送未启用")
        return
    hh, mm = parsed
    scheduler.add_job(
        _auto_push,
        "cron",
        hour=hh,
        minute=mm,
        id="ainews_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("AI 日报推送已注册：每天 %02d:%02d", hh, mm)


_register_job()


# 优先级 4：抢在 chat(5) 之前，避免 "/ainews" 被当成聊天内容
ainews_cmd = on_command(
    "ainews",
    aliases={"AI热点", "ai热点"},
    rule=group_plugin_allowed("ainews"),
    priority=4,
    block=True,
)


@ainews_cmd.handle()
async def handle_ainews(event: MessageEvent, args: Message = CommandArg()):
    sub = str(args).strip()
    if sub == "日报":
        await send_notice(ainews_cmd, event, "⏳ 正在拿今日 AI 日报…")
        daily = await build_daily()
        if daily is None:
            await ainews_cmd.finish("日报源今天没拿到，稍后再试试吧")
        await send_reply(ainews_cmd, event, daily[1], plugin_config.reply_chunk_chars, quote=False)
        await ainews_cmd.finish()

    if sub == "push":
        if not is_superuser(event):
            await ainews_cmd.finish("只有管理员能手动推送")
        await send_notice(ainews_cmd, event, "⏳ 正在拿今日 AI 日报并推送，稍等一会儿…")
        await ainews_cmd.finish(await push_daily(force=True))

    await send_notice(ainews_cmd, event, "⏳ 正在拿 AI 热点榜…")
    hot = await build_hot()
    if hot is None:
        await ainews_cmd.finish("热点源今天没拿到，稍后再试试吧")
    await send_reply(ainews_cmd, event, hot, plugin_config.reply_chunk_chars, quote=False)
    await ainews_cmd.finish()
