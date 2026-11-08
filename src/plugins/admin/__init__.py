"""管理插件：/bot status | clear | off | on | memory | forget，仅 superusers 可用。

群级开关运行时可改：/bot off|on <功能> 写运行时覆盖（重启不丢），
优先于 .env 的 group_plugin_off 默认值。
"""

import asyncio
import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from src.shared import chat_store, group_context, llm, memory
from src.shared.config import plugin_config
from src.shared.storage import (
    PLUGINS,
    get_setting,
    group_plugin_enabled,
    group_plugin_override_id,
    session_key_str,
    set_group_plugin_id,
)
from src.shared.utils import is_superuser, send_reply

_STARTED = time.time()

# 插件的中文名（/bot status 展示用）；英文名是 PLUGINS 里的键
PLUGIN_NAMES = {
    "chat": "聊天",
    "link_parser": "链接",
    "video_parser": "视频",
    "forward_parser": "转发解析",
    "chat_analyze": "群聊分析",
    "quote": "一言",
    "news": "新闻",
    "ainews": "AI热点",
    "group_digest": "晚报",
    "remind": "提醒",
    "rank": "排行",
    "weather": "天气",
    "translate": "翻译",
    "image_gen": "生图",
    "proactive": "主动回复",
}

# /bot off|on 额外接受的叫法
PLUGIN_ALIASES: dict[str, str] = {name: key for key, name in PLUGIN_NAMES.items()}
PLUGIN_ALIASES.update({
    "水群": "rank", "排行榜": "rank", "画图": "image_gen", "画画": "image_gen",
    "主动": "proactive", "主动发言": "proactive", "分析": "chat_analyze",
    "jev": "chat_analyze", "一言bot": "quote",
    "热点": "ainews", "ai新闻": "ainews", "日报": "ainews",
})


def _resolve_plugin(arg: str) -> str | None:
    """中文名/别名/英文键 → 插件键；认不出返回 None。"""
    if arg in PLUGIN_ALIASES:
        return PLUGIN_ALIASES[arg]
    return arg if arg in PLUGINS else None


async def _admin_only(event: MessageEvent) -> bool:
    return is_superuser(event)


bot_cmd = on_command("bot", rule=_admin_only, priority=3, block=True)


@bot_cmd.handle()
async def handle_bot(event: MessageEvent):
    parts = event.get_plaintext().strip().split()
    action = parts[1] if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else ""

    if action == "status":
        lines = [
            f"运行时长：{(time.time() - _STARTED) / 3600:.1f} 小时",
            f"模型：{plugin_config.llm_model} @ {plugin_config.llm_base_url}",
            f"视觉模型：{plugin_config.llm_vision_model or '（关闭）'}",
            f"生图模型：{plugin_config.image_gen_model or '（关闭）'}",
            f"会话记忆：{llm.history_count()} 个",
        ]
        if plugin_config.llm_fallback_api_key:
            health = "正常" if llm.main_healthy() else f"连续失败 {llm.main_failures()} 次"
            lines.append(
                f"备用模型：{plugin_config.llm_fallback_model} "
                f"@ {plugin_config.llm_fallback_base_url or '（官方默认）'}，"
                f"主通道：{health}"
            )
        schedules = []
        if plugin_config.daily_news_time.strip():
            schedules.append(f"新闻 {plugin_config.daily_news_time}")
        if plugin_config.ainews_time.strip():
            schedules.append(f"AI热点 {plugin_config.ainews_time}")
        if plugin_config.digest_time.strip():
            schedules.append(f"晚报 {plugin_config.digest_time}")
        reminders = get_setting("reminders", [])
        pending = len(reminders) if isinstance(reminders, list) else 0
        if pending:
            schedules.append(f"待发提醒 {pending} 条")
        if schedules:
            lines.append("定时任务：" + " · ".join(schedules))
        if isinstance(event, GroupMessageEvent):
            states = []
            for p in PLUGINS:
                state = "开" if group_plugin_enabled(event, p) else "关"
                if group_plugin_override_id(event.group_id, p) is not None:
                    state += "★"  # 手动设置过，优先于 .env 默认
                states.append(f"{PLUGIN_NAMES[p]}={state}")
            lines.append("本群插件：" + "  ".join(states) + "（★=手动设置）")
        await send_reply(bot_cmd, event, "\n".join(lines), plugin_config.reply_chunk_chars, quote=False)
        await bot_cmd.finish()

    if action == "clear":
        if isinstance(event, GroupMessageEvent) and arg == "all":
            n = llm.clear_group(event.group_id)
            group_context.clear(event.group_id)
            await bot_cmd.finish(f"已清空本群 {n} 个会话记忆和群聊上下文 ✓")
        llm.reset_history(session_key_str(event))
        await bot_cmd.finish("已清空当前会话记忆 ✓")

    if action in ("off", "on"):
        if not isinstance(event, GroupMessageEvent):
            await bot_cmd.finish("群级开关要在群里用（作用对象是当前群）")
        if not arg:
            offs = [
                PLUGIN_NAMES[p] for p in PLUGINS if not group_plugin_enabled(event, p)
            ]
            await bot_cmd.finish(
                "本群已关闭：" + ("、".join(offs) if offs else "（无）")
                + "\n用法：/bot off|on <功能>，功能名可用中文，如 /bot off 聊天"
            )
        plugin = _resolve_plugin(arg)
        if plugin is None:
            await bot_cmd.finish(
                f"不认识「{arg}」～ 可以开关的有：{'、'.join(PLUGIN_NAMES.values())}"
            )
        if action == "off":
            set_group_plugin_id(event.group_id, plugin, False)
            await bot_cmd.finish(f"已在本群关闭：{PLUGIN_NAMES[plugin]} ✓")
        set_group_plugin_id(event.group_id, plugin, True)
        await bot_cmd.finish(f"已在本群开启：{PLUGIN_NAMES[plugin]} ✓")

    if arg.isdigit() and len(arg) > 15:
        # 超长数字串会让 SQLite 整型绑定抛 OverflowError，提前拦下
        await bot_cmd.finish("QQ 号参数太长了，检查一下输入")

    if action == "memory":
        target = int(arg) if arg.isdigit() else event.user_id
        p = await asyncio.to_thread(memory.get_profile, target, 20)
        lines = [
            f"QQ {target} 的档案：{p['nickname'] or '（未记录昵称）'}",
            f"首次互动：{p['first_seen'] or '—'}",
            f"最近互动：{p['last_seen'] or '—'}",
            f"对话次数：{p['chat_count']}",
            "已知信息：",
            *( [f"- {f}" for f in p["facts"]] or ["（暂无）"] ),
        ]
        await send_reply(bot_cmd, event, "\n".join(lines), plugin_config.reply_chunk_chars, quote=False)
        await bot_cmd.finish()

    if action == "forget":
        target = int(arg) if arg.isdigit() else event.user_id
        m = await asyncio.to_thread(memory.forget_user, target)
        s = await asyncio.to_thread(chat_store.forget_user_sessions, target)
        await bot_cmd.finish(
            f"已清除 QQ {target} 的记忆 ✓（事实 {m['facts']} 条、档案、"
            f"活跃 {m['activity']} 条、对话历史 {s} 段）"
        )

    await bot_cmd.finish(
        "用法：/bot status\n"
        "/bot clear [all]  清会话记忆\n"
        "/bot off|on <功能>  群级开关，如 /bot off 聊天、/bot on 生图\n"
        "/bot memory [QQ]  查看长期记忆\n"
        "/bot forget [QQ]  删除长期记忆\n"
        "手动开关优先于 .env 的 group_plugin_off 默认值，重启不丢"
    )
