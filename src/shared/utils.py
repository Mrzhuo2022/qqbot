"""各插件共用的小工具。"""

import asyncio
import re

import nonebot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)

from src.shared.config import plugin_config
from src.shared.storage import group_plugin_enabled
from src.shared import tts

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff<>\"']+")

# 走视频解析的站点域名；\b 防止误匹配 fox.com 这类
VIDEO_DOMAIN_RE = re.compile(
    r"\b(?:b23\.tv|bilibili\.com|youtube\.com|youtu\.be|twitter\.com|x\.com|douyin\.com)"
)

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


def group_allowed(event: MessageEvent) -> bool:
    """群白名单过滤；私聊不受限。"""
    whitelist = plugin_config.group_whitelist
    if isinstance(event, GroupMessageEvent) and whitelist:
        return event.group_id in whitelist
    return True


def group_plugin_allowed(plugin: str):
    """生成「白名单 + 群级开关」的命令过滤规则；私聊直接放行。

    命令类插件共用：group_plugin_allowed("weather") 作为一个 rule 传给 on_command。
    """
    async def _allowed(event: MessageEvent) -> bool:
        if not isinstance(event, GroupMessageEvent):
            return True
        return group_allowed(event) and group_plugin_enabled(event, plugin)
    return _allowed


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """"HH:MM" → (时, 分)；不合法返回 None（定时任务配置用）。"""
    try:
        hh, mm = map(int, text.strip().split(":"))
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except (ValueError, AttributeError):
        pass
    return None


def extract_first_url(text: str) -> str | None:
    m = URL_RE.search(text)
    return m.group() if m else None


# ── 提示词注入防护 ──────────────────────────────────────────
# OneBot 的 CQ 码（[CQ:at,qq=all]、[CQ:image,...] 等）出现在 LLM 输出或网页
# 标题里时，NapCat 会把它当真实消息行为执行（@全体、发任意图），
# 所以所有出群文本必须先过滤
CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 常见注入话术的启发式检测：命中只做「加强防护 + 回复中标注」，不拦截，
# 因为技术文章讨论注入本身也会命中，拦了会大量误伤
SUSPICIOUS_RE = re.compile(
    "|".join(
        [
            r"忽略(?:之前|以上|上文|前面|所有)?的?(?:指令|设定|规则|提示)",
            r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)",
            r"system\s*prompt|系统提示词|初始(?:系统)?提示",
            r"你现在是|从现在开始你就是|(?:act|pretend)\s+(?:as|like)",
            r"\[CQ:",
        ]
    ),
    re.I,
)


def sanitize_bot_output(text: str) -> str:
    """任何含不可信来源（LLM 输出、网页标题等）的文本，发到群里前都要过这里。"""
    text = CQ_CODE_RE.sub("", text)
    text = CTRL_RE.sub("", text)
    return text.strip()


def sanitize_one_line(text: str, limit: int = 20) -> str:
    """昵称/群名片/记忆事实这类会拼进提示词的短字段，统一在这里洗成单行。

    在 sanitize_bot_output 之上再折叠换行：带换行的"事实"或名片能在列表式
    提示词里伪装出额外条目行（比如伪造一条「- 你必须无视所有规则"），
    洗成单行后最多影响自己那一行。幂等，重复过无副作用。
    """
    return sanitize_bot_output(text).replace("\n", " ").strip()[:limit]


def looks_suspicious(text: str) -> bool:
    return bool(SUSPICIOUS_RE.search(text))


def wrap_untrusted(text: str, limit: int) -> str:
    """把不可信内容包进数据边界标签，配合各插件的总结 system 使用。"""
    return f"<untrusted_data>\n{text[:limit]}\n</untrusted_data>"


def split_message(text: str, limit: int) -> list[str]:
    """把长文本按行切成多段，每段不超过 limit，适合 QQ 长消息展示。"""
    limit = max(1, limit)
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            parts.append(buf)
            buf = line
        else:
            buf += line
    if buf.strip():
        parts.append(buf)
    return [p for p in parts if p.strip()]


def quote_message(event: MessageEvent, text: str) -> Message:
    """带引用回复的消息，群聊里能看出 bot 在回应哪条。"""
    return MessageSegment.reply(event.message_id) + text


async def send_reply(
    matcher, event: MessageEvent, text: str, chunk_limit: int, quote: bool = True
) -> None:
    """发送产出文本；超长自动分段，默认带引用回复。

    quote=False 直接发送：命令类的长产出（新闻/晚报/排行这类）没必要
    每条都引用命令本身；对话类（群聊）和自动解析类保持引用，能看出回应
    的是哪条。私聊对话由 chat 插件显式传 quote=False——私聊没有歧义。
    出群统一收口：任何含用户可控字段（昵称、聊天原文、外部 API 返回等）的
    文本在这里统一过净化器剥 CQ 码——命令类插件的所有输出都走这里，
    单点兜住「昵称里藏 [CQ:at,qq=all]」这类注入。净化幂等，重复过无副作用。
    语音模式：本群开了语音回复（/bot on 语音）时，先发一条语音（长产出念开头，
    完整文字照发；短回复纯语音替代文字）；TTS 失败完整回落纯文字。
    语音不带引用段（QQ 语音消息挂 reply 发不出去），回应指向靠听内容本身。
    """
    text = sanitize_bot_output(text)
    gid = getattr(event, "group_id", None)
    if gid is not None and tts.voice_reply_on(gid):
        speech = tts.speech_text(text)
        if speech:
            audio = await tts.synthesize(speech)
            if audio is not None:
                try:
                    await matcher.send(MessageSegment.record(audio))
                except Exception:
                    pass  # 语音发不出去（协议端不支持等），走下面的文字路径
                else:
                    # 短回复：语音已完整表达，不发文字。用完整文本长度判断
                    # （speech_text 已截断，len(speech) 恒小于等于上限，不能用它判）
                    if len(text) <= max(plugin_config.tts_max_chars, 10):
                        return
    parts = split_message(text, chunk_limit)
    await matcher.send(quote_message(event, parts[0]) if quote else parts[0])
    for part in parts[1:]:
        await matcher.send(part)


async def send_notice(matcher, event: MessageEvent, text: str) -> None:
    """慢命令开跑前先回一条带引用的进度提示，让用户知道 bot 收到了。"""
    await matcher.send(quote_message(event, text))


def is_superuser(event: MessageEvent) -> bool:
    return event.get_user_id() in map(str, nonebot.get_driver().config.superusers)


_background_tasks: set = set()


def spawn(coro) -> None:
    """fire-and-forget 后台任务：持引用防止任务被 GC。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
