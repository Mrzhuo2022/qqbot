"""翻译插件：/translate 文本（/翻译 同义）——LLM 直译，只回译文。

- 第一个词是语言代码时可指定目标语言：/translate en 你好 → Hello
  （支持 en/ja/ko/fr/de/es/ru/zh）；不指定时中文译英语、其他译中文
- 译文只过 LLM 输出净化（CQ 码/控制字符），不携带任何不可信第三方内容
- 按群限流（translate_rate_per_min）+ 群级开关 /bot off translate
"""

import logging

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg

from src.shared import llm
from src.shared.config import plugin_config
from src.shared.rate_limit import RateLimiter
from src.shared.utils import clip, group_plugin_allowed, send_notice, send_reply

logger = logging.getLogger("translate")

MAX_INPUT = 500

LANGS = {
    "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语",
    "de": "德语", "es": "西班牙语", "ru": "俄语", "zh": "简体中文",
}

SYSTEM_TMPL = (
    "你是翻译器。把用户消息翻译成{target}，只输出译文本身，"
    "不要解释、不要加引号、不要输出原文。"
)
AUTO_TARGET = "简体中文（若原文本身是中文，则翻译成英语）"
USAGE = (
    "用法：/translate [语言] 文本，如 /translate 你好、/translate en 早安\n"
    "语言可选：en ja ko fr de es ru zh（不填=中英互译）"
)

_limiter = RateLimiter(plugin_config.translate_rate_per_min, 60)


def parse_target(args_text: str) -> tuple[str, str] | None:
    """命令参数 → (目标语言描述, 待翻译文本)；没有可翻文本返回 None。"""
    args_text = args_text.strip()
    if not args_text:
        return None
    tokens = args_text.split(maxsplit=1)
    code = tokens[0].lower()
    if code in LANGS:
        if len(tokens) > 1:
            return LANGS[code], tokens[1].strip()
        return None  # 只给了语言代码没给文本 → 用法提示
    return AUTO_TARGET, args_text


# 优先级 4：抢在 chat(5) 之前，避免 "/translate" 被当成聊天内容
translate_cmd = on_command(
    "translate", aliases={"翻译"}, rule=group_plugin_allowed("translate"), priority=4, block=True
)


@translate_cmd.handle()
async def handle_translate(event: MessageEvent, args: Message = CommandArg()):
    parsed = parse_target(str(args))
    if parsed is None:
        await translate_cmd.finish(USAGE)
    target, text = parsed
    text = text[:MAX_INPUT]

    if not llm.llm_ready():
        await translate_cmd.finish("AI 还没配置好，翻译暂时用不了")
    if not _limiter.check(
        event.group_id if isinstance(event, GroupMessageEvent) else f"u:{event.user_id}"
    ):
        await translate_cmd.finish("翻得太频繁啦，一分钟后再试")

    await send_notice(translate_cmd, event, "⏳ 正在翻译…")
    try:
        reply = await llm.llm_chat(SYSTEM_TMPL.format(target=target), text)
    except Exception:
        logger.exception("翻译失败")
        await translate_cmd.finish("翻译出错了，稍后再试")
    await send_reply(
        translate_cmd, event, clip(reply, plugin_config.reply_max_chars),
        plugin_config.reply_chunk_chars, quote=False,
    )
    await translate_cmd.finish()
