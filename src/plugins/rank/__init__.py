"""水群排行插件：/rank 看本群今日（/rank week 近 7 天）谁最能聊。

今日榜优先从 QQ 侧聊天记录统计（bot 掉线/重启期间的也能算上），
接口失败退回本地路过统计（memory.activity，每群每人每天一行）；
周榜只有本地统计。不调用 LLM；活跃行按天滚动保留 35 天。
`/排行` 为同义别名；群级开关 /bot off rank。
"""

import asyncio
import datetime as dt
import logging

from nonebot import on_command, require
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from src.shared import history, memory
from src.shared.config import plugin_config
from src.shared.utils import group_plugin_allowed, send_reply

logger = logging.getLogger("rank")

TOP_N = 10
MEDALS = ("🥇", "🥈", "🥉")
KEEP_DAYS = 35
# QQ 侧拉取的历史消息条数上限（协议端通常还会再截）
RANK_HISTORY_COUNT = 300


async def _cleanup_job() -> None:
    removed = memory.prune_activity(KEEP_DAYS)
    if removed:
        logger.info("活跃统计清理：删除 %d 行过期记录", removed)


def _register_cleanup() -> None:
    # 每天 04:30 清理 + 调度器启动时立即补清一次
    # （放 job 里而不是导入时直接跑：单测导入本模块不该写真实库）
    scheduler.add_job(
        _cleanup_job, "cron", hour=4, minute=30,
        id="rank_cleanup", replace_existing=True,
    )
    scheduler.add_job(
        _cleanup_job, "date", run_date=dt.datetime.now(),
        id="rank_cleanup_startup", replace_existing=True,
    )


_register_cleanup()


def format_rank(rows: list[dict], total: int, days: int) -> str:
    """排行数据 → 出群文案。"""
    scope = "今日" if days == 1 else f"近 {days} 天"
    if not rows:
        return f"{scope}本群还没有聊天记录，先聊几句再来～"
    lines = [f"📊 {scope}水群排行（共 {total} 条消息）"]
    for i, r in enumerate(rows):
        medal = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        name = r["nickname"] or str(r["user_id"])
        lines.append(f"{medal} {name}：{r['msgs']} 条")
    top = rows[0]
    share = top["msgs"] * 100 // max(total, 1)
    lines.append(f"\n🏆 冠军贡献了本群 {share}% 的话")
    return "\n".join(lines)


# 优先级 4：抢在 chat(5) 之前，避免 "/rank" 被当成聊天内容
rank_cmd = on_command(
    "rank", aliases={"排行"}, rule=group_plugin_allowed("rank"), priority=4, block=True
)


@rank_cmd.handle()
async def handle_rank(event: MessageEvent, bot: Bot, args: Message = CommandArg()):
    if not isinstance(event, GroupMessageEvent):
        await rank_cmd.finish("排行榜要看群聊数据，请在群里使用")
    days = 7 if str(args).strip().lower() in ("week", "w", "周") else 1

    # 今日榜优先查 QQ 侧聊天记录（不依赖本地记忆开关）：bot 不在线/重启
    # 漏记的消息也能算上；接口拉不到（协议端不支持等）再退回本地路过统计
    if days == 1:
        since_ts = int(dt.datetime.combine(dt.date.today(), dt.time.min).timestamp())
        tally = await history.fetch_group_activity(
            bot, event.group_id, event.self_id, RANK_HISTORY_COUNT, since_ts
        )
        if tally is not None:
            top = sorted(tally.items(), key=lambda kv: kv[1]["msgs"], reverse=True)[:TOP_N]
            rows = [
                {"user_id": uid, "nickname": v["nickname"], "msgs": v["msgs"]}
                for uid, v in top
            ]
            total = sum(v["msgs"] for v in tally.values())
            await send_reply(
                rank_cmd, event, format_rank(rows, total, days),
                plugin_config.reply_chunk_chars, quote=False,
            )
            await rank_cmd.finish()

    # 走本地统计前才需要 memory_enabled（活跃表归长期记忆管）
    if not plugin_config.memory_enabled:
        missed = "，QQ 聊天记录也没拉到" if days == 1 else ""
        await rank_cmd.finish(f"本地活跃统计没开{missed}，统计不了")

    rows, total = await asyncio.to_thread(
        lambda: (memory.top_talkers(event.group_id, days, TOP_N),
                 memory.group_msg_total(event.group_id, days))
    )
    await send_reply(
        rank_cmd, event, format_rank(rows, total, days),
        plugin_config.reply_chunk_chars, quote=False,
    )
    await rank_cmd.finish()
