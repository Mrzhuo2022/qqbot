"""HN 日报插件：/hndaily 看本机 hn-daily 项目的每日科技精选，每天定时推送。

- hndaily_time：推送时间 HH:MM，留空关闭定时（/hndaily 命令仍可用）；
  要配在 hn-daily 的生成时间之后（生成时间在 hn-daily 那边配），正常到点就有报
- hndaily_groups：推送目标群号，留空跟随 group_whitelist
- hndaily_users：推送目标个人 QQ 号（私聊），和群号可同时配
- hndaily_json：hn-daily 落盘的 API payload（output/api/latest.json），同机部署直接读文件
- /hndaily：手动看最新一期；/hndaily push：管理员立即推送
- 群级开关 /bot off HN日报：某群退出定时推送，/hndaily 命令在该群也不可用
- 消息是纯文本（QQ 不渲染 Markdown）；每天只推一次（按报告日期去重，落盘重启不重复推）；
  到点发现报告还不是今天的，只吸收生成的小晚点（重试几次），真没报说明
  hn-daily 挂了——多等无济于事，这边修不了，那边修好后 /hndaily push 手动补推
- 报告内容含抓取的网页文本，按不可信数据处理（出群统一过净化器）
"""

import asyncio
import datetime as dt
import json
import logging

from nonebot import get_bot, on_command, require
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from src.shared.config import plugin_config
from src.shared.storage import get_setting, group_plugin_enabled_id, set_setting
from src.shared.utils import (
    clip,
    group_plugin_allowed,
    is_superuser,
    parse_hhmm,
    sanitize_bot_output,
    send_notice,
    send_reply,
)

logger = logging.getLogger("hndaily")

CN_TZ = dt.timezone(dt.timedelta(hours=8))


def _now_cn() -> dt.datetime:
    return dt.datetime.now(CN_TZ)


def _today_cn() -> str:
    return _now_cn().date().isoformat()


def _load_report() -> dict | None:
    """读 hn-daily 落盘的 API payload；文件缺失/坏 JSON/没有日期都算读不到。"""
    try:
        with open(plugin_config.hndaily_json, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("HN 日报读取失败 %s: %s", plugin_config.hndaily_json, e)
        return None
    if not isinstance(report, dict) or not str(report.get("date", "")).strip():
        return None
    return report


def _fmt_report(report: dict) -> str:
    """API payload → QQ 纯文本；坏条目跳过，元字段（id/generated_at）不进正文。"""
    lines = [f"📰 HN 每日科技精选 · {report.get('date')}"]
    theme = str(report.get("theme", "")).strip()
    if theme:
        lines.append(theme)
    n = 0
    for x in report.get("items") or []:
        if not isinstance(x, dict):
            continue
        title = str(x.get("title") or x.get("title_original") or "").strip()
        if not title:
            continue
        n += 1
        lines += ["", f"{n}. {title} ⭐{x.get('score', 0)} 💬{x.get('comments', 0)}"]
        for p in x.get("points") or []:
            p = str(p).strip()
            if p:
                lines.append(f"· {p}")
        comment = str(x.get("comment", "")).strip()
        if comment:
            lines.append(f"💬 {comment}")
        url = str(x.get("url") or x.get("hn_link") or "").strip()
        if url:
            lines.append(url)
    summary = [str(s).strip() for s in report.get("summary") or [] if str(s).strip()]
    if summary:
        lines += ["", "📋 今日总结", *(f"· {s}" for s in summary)]
    persona = str(report.get("persona", "")).strip()
    if persona:
        lines += ["", persona]
    return clip(sanitize_bot_output("\n".join(lines)), plugin_config.reply_max_chars)


def build_latest() -> tuple[str, str] | None:
    """返回 (报告日期, 格式化文本)；读不到返回 None。"""
    report = _load_report()
    if report is None:
        return None
    return str(report.get("date")), _fmt_report(report)


async def _push_daily(force: bool = False) -> tuple[int, str]:
    """推送核心，返回 (成功数, 描述)；提前退出（已推过/无目标）成功数为 -1。"""
    group_targets = [
        g
        for g in (plugin_config.hndaily_groups or plugin_config.group_whitelist)
        if group_plugin_enabled_id(g, "hndaily")
    ]
    user_targets = plugin_config.hndaily_users
    if not group_targets and not user_targets:
        return -1, "没有推送目标：请配置 hndaily_groups/hndaily_users 或 group_whitelist"
    latest = build_latest()
    if latest is None:
        return 0, "HN 日报读不到（文件缺失或格式不对），稍后再试"
    date, text = latest
    # 定时推送只认今天的报告：hn-daily 还没跑完就等着，别把昨天的推出去
    if not force and date != _today_cn():
        return 0, f"HN 日报还不是今天的（最新 {date}），等 hn-daily 跑完再推"
    if not force and get_setting("hndaily:last_push") == date:
        return -1, "今天的 HN 日报已经推送过了"
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
            logger.exception("HN 日报推送到群 %s 失败", gid)
        await asyncio.sleep(1)
    for uid in user_targets:
        try:
            await bot.call_api("send_private_msg", user_id=uid, message=text)
            ok += 1
        except Exception:
            logger.exception("HN 日报推送给 QQ %s 失败", uid)
        await asyncio.sleep(1)
    if ok:
        # 一个目标都没发出去就不记标记，稍后还能 /hndaily push 重试
        set_setting("hndaily:last_push", date)
    return ok, f"已推送到 {ok}/{total} 个目标"


async def push_daily(force: bool = False) -> str:
    return (await _push_daily(force))[1]


# 定时推送全军覆没时（生成晚点/bot 掉线）的自动重试：每 30 分钟一次，最多 3 次。
# 推送时间本就该配在生成时间之后，到点还没报多半是 hn-daily 挂了——
# 多等无济于事（这边 /hndaily 影响不了那边生成），超时放弃，修好后手动补推
PUSH_RETRIES = 3
RETRY_DELAY_MIN = 30


async def _auto_push(retries: int = 0) -> None:
    ok, msg = await _push_daily()
    logger.info("HN 日报推送：%s", msg)
    if ok == 0 and retries < PUSH_RETRIES:
        run_at = dt.datetime.now() + dt.timedelta(minutes=RETRY_DELAY_MIN)
        scheduler.add_job(
            _auto_push, "date", run_date=run_at, args=[retries + 1],
            id="hndaily_retry", replace_existing=True,
        )
        logger.info("全部失败，%s 自动重试（第 %d/%d 次）",
                    run_at.strftime("%H:%M"), retries + 1, PUSH_RETRIES)


def _register_job() -> None:
    parsed = parse_hhmm(plugin_config.hndaily_time)
    if parsed is None:
        if plugin_config.hndaily_time.strip():
            logger.error("hndaily_time 应为 HH:MM 格式，定时推送未启用")
        return
    hh, mm = parsed
    scheduler.add_job(
        _auto_push,
        "cron",
        hour=hh,
        minute=mm,
        id="hndaily_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("HN 日报推送已注册：每天 %02d:%02d", hh, mm)


_register_job()


# 优先级 4：抢在 chat(5) 之前，避免 "/hndaily" 被当成聊天内容
hndaily_cmd = on_command(
    "hndaily",
    aliases={"hn日报", "HN日报"},
    rule=group_plugin_allowed("hndaily"),
    priority=4,
    block=True,
)


@hndaily_cmd.handle()
async def handle_hndaily(event: MessageEvent, args: Message = CommandArg()):
    sub = str(args).strip()
    if sub == "push":
        if not is_superuser(event):
            await hndaily_cmd.finish("只有管理员能手动推送")
        await send_notice(hndaily_cmd, event, "⏳ 正在读 HN 日报并推送，稍等一会儿…")
        await hndaily_cmd.finish(await push_daily(force=True))

    await send_notice(hndaily_cmd, event, "⏳ 正在读 HN 日报…")
    latest = build_latest()
    if latest is None:
        await hndaily_cmd.finish("HN 日报还没生成，检查 hn-daily 是否跑过")
    date, text = latest
    if date != _today_cn():
        text = f"（最新一期是 {date}，今天的还没生成）\n\n{text}"
    await send_reply(hndaily_cmd, event, text, plugin_config.reply_chunk_chars, quote=False)
    await hndaily_cmd.finish()
