"""翻译插件：/translate 文本（/翻译 同义）——LLM 直译，只回译文。

- 第一个词是语言代码时可指定目标语言：/translate en 你好 → Hello
  （支持 en/ja/ko/fr/de/es/ru/zh）；不指定时中文译英语、其他译中文
- 自然语言也能触发：「把早上好翻译成日语」「翻译成英语：hello」
  （parse_nl_translate，只认祈使句式，问句不触发；未配置 AI 时保持沉默）
- 译文只过 LLM 输出净化（CQ 码/控制字符），不携带任何不可信第三方内容
- 按群限流（translate_rate_per_min）+ 群级开关 /bot off translate
"""

import logging
import re

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.rule import Rule

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


# ── 自然语言触发：「把X翻译成Y」「翻译成Y：X」────────────────

# 中文语言名 → LANGS 的码（自然语言里没人说语言代码）
_LANG_SYNONYMS = {
    "英文": "en", "英语": "en", "日文": "ja", "日语": "ja",
    "韩文": "ko", "韩语": "ko", "法文": "fr", "法语": "fr",
    "德文": "de", "德语": "de", "西班牙文": "es", "西班牙语": "es",
    "俄文": "ru", "俄语": "ru", "中文": "zh", "汉语": "zh",
}
_LANG_ALT = "|".join(_LANG_SYNONYMS)

# 形式一：把 X 翻译/翻 成 Y（整句；text 与动词之间必须有内容）
_NL_TR_GIVE = re.compile(
    rf"^(?:帮我|给我)?把(?P<text>.{{1,200}}?)翻(?:译)?(?:成|为)"
    rf"(?P<lang>{_LANG_ALT})[，。！!～~\s]*$"
)
# 形式二：翻译成 Y：X（必须有冒号，防「翻译成日语的方法」这类闲聊误触发；
# 「翻译成/为」与「译」都可省略：「翻译日语：早上好」「翻日语：X」也成立）
_NL_TR_COLON = re.compile(
    rf"^翻(?:译)?(?:成|为)?(?P<lang>{_LANG_ALT})[：:]\s*(?P<text>.+)$"
)


def parse_nl_translate(text: str) -> tuple[str, str] | None:
    """自然语言翻译解析：命中返回 (目标语言描述, 待翻译文本)，否则 None。

    只认上面两种显式祈使句式；问句（含 ？）和命令（/ 开头）不触发。
    返回类型与 parse_target 一致。
    """
    text = text.strip()
    if not text or text.startswith("/") or "？" in text:
        return None
    m = _NL_TR_GIVE.match(text) or _NL_TR_COLON.match(text)
    if not m:
        return None
    body = m.group("text").strip().strip("，。！!～~、 ")
    if not body:
        return None
    return LANGS[_LANG_SYNONYMS[m.group("lang")]], body[:MAX_INPUT]


def _nl_translate_ready(event: MessageEvent) -> bool:
    """内容规则：LLM 可用且消息是祈使式翻译请求；未配置时保持沉默不接话。"""
    return llm.llm_ready() and parse_nl_translate(event.get_plaintext()) is not None


# 同为优先级 4：命令 matcher 先注册，"/翻译 ..." 会被命令先吃掉不会重复触发
nl_translate = on_message(
    rule=Rule(group_plugin_allowed("translate"), _nl_translate_ready),
    priority=4, block=True,
)


@nl_translate.handle()
async def handle_nl_translate(event: MessageEvent):
    parsed = parse_nl_translate(event.get_plaintext())
    if parsed is not None:
        await _do_translate(nl_translate, event, *parsed)
    await nl_translate.finish()


async def _do_translate(matcher, event: MessageEvent, target: str, text: str) -> None:
    """LLM 翻译并回译文：命令与自然语言触发共用（限流/进度提示/文案一致）。"""
    if not _limiter.check(
        event.group_id if isinstance(event, GroupMessageEvent) else f"u:{event.user_id}"
    ):
        await matcher.finish("翻得太频繁啦，一分钟后再试")

    await send_notice(matcher, event, "⏳ 正在翻译…")
    try:
        reply = await llm.llm_chat(SYSTEM_TMPL.format(target=target), text)
    except Exception:
        logger.exception("翻译失败")
        await matcher.finish("翻译出错了，稍后再试")
    await send_reply(
        matcher, event, clip(reply, plugin_config.reply_max_chars),
        plugin_config.reply_chunk_chars, quote=False,
    )
    await matcher.finish()


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
    if not llm.llm_ready():
        await translate_cmd.finish("AI 还没配置好，翻译暂时用不了")
    await _do_translate(translate_cmd, event, target, text)
