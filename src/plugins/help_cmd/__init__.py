"""帮助命令：/help 列出所有可用命令（/帮助 同义）。

命令越来越多，群友在群里就能查；管理员额外多一段管理命令。
文案是手工维护的清单——加新命令时记得同步这里和 README。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from src.shared.config import plugin_config
from src.shared.utils import is_superuser, send_reply

HELP_TEXT = (
    "📖 我会的事（命令不用 @，群里直接发 / 开头即可）：\n"
    "@我 + 文字/图片 —— 聊天（带记忆）；发链接/视频/合并转发会自动总结\n"
    "/help /帮助 —— 这个帮助\n"
    "/reset —— 清空和我的对话记忆\n"
    "/profile —— 查看我记得你什么\n"
    "/forget —— 删掉我记住的你的信息\n"
    "/jev [N] /分析 —— 分析最近群聊（默认 1 条，/jev 5 看整体）\n"
    "/news —— 今日 60 秒新闻\n"
    "/ainews /AI热点 —— AI 热点 Top10（日报 看今日日报）\n"
    "/hndaily /hn日报 —— HN 每日科技精选（读本机 hn-daily）\n"
    "/search 关键词 —— 联网搜索（@我 带「联网」「搜一下」也能触发）\n"
    "/quote /一言 —— 随机一句话\n"
    "/digest /晚报 —— 预览今晚的群聊晚报\n"
    "/remind 30min 事 —— 定时提醒（list 查看 / cancel 编号 取消）\n"
    "/rank /排行 —— 今日水群排行（week 看近 7 天）\n"
    "/weather 城市 /天气 —— 查天气（今明两天）\n"
    "/translate [语言] 文 /翻译 —— 翻译（不填语言=中英互译）\n"
    "/image 描述 /生图 —— AI 生图（需配置图像生成服务）"
)

ADMIN_TEXT = (
    "\n\n🛠 管理员专属：\n"
    "/bot status —— 运行状态（含本群各功能开关）\n"
    "/bot clear [all] —— 清会话记忆\n"
    "/bot off|on <功能> —— 群级开关（如 /bot off 聊天）\n"
    "/bot memory|forget [QQ] —— 查/删长期记忆\n"
    "/news push · /ainews push · /digest push · /hndaily push —— 立即推送"
)


def build_help(is_admin: bool) -> str:
    return HELP_TEXT + (ADMIN_TEXT if is_admin else "")


# 优先级 4：抢在 chat(5) 之前，避免 "/help" 被当成聊天内容
help_cmd = on_command(
    "help", aliases={"帮助"}, priority=4, block=True
)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    text = build_help(is_superuser(event))
    await send_reply(help_cmd, event, text, plugin_config.reply_chunk_chars, quote=False)
    await help_cmd.finish()
