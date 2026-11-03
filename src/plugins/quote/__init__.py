"""一言插件：/quote 随机发一句 hi.logacg.com 的句子。

- 主源 hi.logacg.com，官方 v1.hitokoto.cn 兜底（同一 JSON 格式）
- 句子来自社区句库，出群前过 sanitize_bot_output；不进 LLM，无注入面
- 群级开关接入 /bot on|off（storage.PLUGINS 里的 "quote"）
"""

import logging

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from src.shared.config import plugin_config
from src.shared.utils import (
    UA_HEADERS,
    clip,
    group_plugin_allowed,
    sanitize_bot_output,
    send_reply,
)

logger = logging.getLogger("quote")

QUOTE_APIS = [
    "https://hi.logacg.com/",
    "https://v1.hitokoto.cn/",
]

# hitokoto 句库的分类代号（type 字段）
CATEGORY = {
    "a": "动画",
    "b": "漫画",
    "c": "游戏",
    "d": "文学",
    "e": "原创",
    "f": "网络",
    "g": "其他",
    "h": "影视",
    "i": "诗词",
    "j": "网易云",
    "k": "哲学",
}


def format_quote(data: dict) -> str | None:
    """把一言 JSON 排成卡片；没有句子正文返回 None。"""
    text = str(data.get("hitokoto") or "").strip()
    if not text:
        return None
    lines = [f"「{text}」"]
    source = str(data.get("from") or "").strip()
    author = str(data.get("from_who") or "").strip()
    tails = []
    if author and source and author != source:
        tails.append(f"{author}《{source}》")
    elif source:
        tails.append(f"《{source}》")
    elif author:
        tails.append(author)
    cat = CATEGORY.get(str(data.get("type") or ""))
    if cat:
        tails.append(cat)
    if tails:
        lines.append("—— " + " · ".join(tails))
    return "\n".join(lines)


async def fetch_quote() -> dict | None:
    async with httpx.AsyncClient(timeout=10, headers=UA_HEADERS) as client:
        for url in QUOTE_APIS:
            try:
                resp = await client.get(url)
                data = resp.json()
                if isinstance(data, dict) and str(data.get("hitokoto") or "").strip():
                    return data
            except Exception as e:
                logger.warning("一言源失败 %s: %s", url, e)
    return None


quote_cmd = on_command(
    "quote", aliases={"一言"}, rule=group_plugin_allowed("quote"), priority=4, block=True
)


@quote_cmd.handle()
async def handle_quote(event: MessageEvent):
    data = await fetch_quote()
    if data is None:
        await quote_cmd.finish("一言源暂时都挂了，稍后再试试吧")
    card = format_quote(data)
    if card is None:
        await quote_cmd.finish("一言源返回了空句子，稍后再试试吧")
    card = clip(sanitize_bot_output(card), plugin_config.reply_max_chars)
    await send_reply(quote_cmd, event, card, plugin_config.reply_chunk_chars, quote=False)
    await quote_cmd.finish()
