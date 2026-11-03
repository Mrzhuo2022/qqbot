"""记忆自助管理：/profile 查看自己的档案，/forget 清空自己被记住的信息。"""

import asyncio

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from src.shared import chat_store, memory
from src.shared.config import plugin_config
from src.shared.utils import send_reply


async def _memory_on(event: MessageEvent) -> bool:
    return plugin_config.memory_enabled


profile_cmd = on_command("profile", rule=_memory_on, priority=4, block=True)


@profile_cmd.handle()
async def handle_profile(event: MessageEvent):
    p = await asyncio.to_thread(memory.get_profile, event.user_id, 20)
    lines = [
        f"你的档案（QQ {p['user_id']}）",
        f"昵称：{p['nickname'] or '（未记录）'}",
        f"对话次数：{p['chat_count']}",
        f"首次互动：{p['first_seen'] or '—'}",
        f"最近互动：{p['last_seen'] or '—'}",
        "",
        "我记得的关于你：",
        *( [f"- {f}" for f in p["facts"]] or ["（暂无，多聊聊我就会记住）"] ),
    ]
    await send_reply(profile_cmd, event, "\n".join(lines), plugin_config.reply_chunk_chars, quote=False)
    await profile_cmd.finish()


forget_cmd = on_command("forget", rule=_memory_on, priority=4, block=True)


@forget_cmd.handle()
async def handle_forget(event: MessageEvent):
    """完整清除：事实 + 成员档案 + 活跃统计 + 和我的全部对话历史。"""
    m = await asyncio.to_thread(memory.forget_user, event.user_id)
    s = await asyncio.to_thread(chat_store.forget_user_sessions, event.user_id)
    total = m["facts"] + m["member"] + m["activity"] + s
    if total == 0:
        await forget_cmd.finish("本来就没记住你什么，放心 ✓")
    parts = ["已清除关于你的记忆 ✓"]
    if m["facts"]:
        parts.append(f"事实 {m['facts']} 条")
    if m["member"]:
        parts.append("成员档案")
    if m["activity"]:
        parts.append(f"活跃记录 {m['activity']} 条")
    if s:
        parts.append(f"对话历史 {s} 段")
    await forget_cmd.finish("，".join(parts) + " ✓")
