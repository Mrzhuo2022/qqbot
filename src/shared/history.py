"""QQ 侧群历史消息的拉取与解析（get_group_msg_history）。

chat_analyze 的 /jev 与 group_digest 的晚报都用这里补拉消息：
本地缓冲（group_context）只有最近 50 条且重启即空，QQ 侧才是全量来源。
"""

import logging

from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message

logger = logging.getLogger("history")

# 群历史里的媒体消息记占位标签，和缓冲里 chat 插件的记法一致
MEDIA_LABELS = {
    "image": "[图片]", "record": "[语音]", "video": "[视频]",
    "file": "[文件]", "forward": "[合并转发]", "json": "[卡片]",
}


def segments_text(segs) -> str:
    """OneBot 消息段 → 纯文本（媒体用占位标签），无文字返回空串。

    segs 可能是 dict 数组（JSON 响应）或 Message 对象（CQ 码解析结果）。
    """
    if isinstance(segs, str):
        try:
            segs = Message(segs)
        except Exception:
            return ""
    parts = []
    for seg in segs or []:
        if isinstance(seg, dict):
            stype, data = seg.get("type"), seg.get("data") or {}
        else:
            stype = getattr(seg, "type", None)
            data = getattr(seg, "data", None) or {}
        if stype == "text":
            parts.append(str(data.get("text", "")))
        elif stype in MEDIA_LABELS:
            parts.append(MEDIA_LABELS[stype])
    return "".join(parts).strip()


async def fetch_group_lines(
    bot: Bot,
    group_id: int,
    self_id,
    count: int,
    since_ts: int | None = None,
    exclude_message_id=None,
) -> list[str] | None:
    """拉群最近消息整理成「谁：内容」行（旧→新）。

    过滤 bot 自己的消息、/ 开头的命令和空消息，媒体段记占位标签；
    since_ts 不为 None 时只保留该 Unix 时间戳之后的消息（按天过滤用）。
    接口调用失败返回 None、当天确实没消息返回 []——调用方据此决定
    是否回退本地缓冲。
    """
    try:
        resp = await bot.call_api("get_group_msg_history", group_id=group_id, count=count)
    except Exception as e:
        logger.warning("拉取群 %s 历史消息失败: %s", group_id, e)
        return None
    lines = []
    for m in resp.get("messages") or []:
        if not isinstance(m, dict) or m.get("message_id") == exclude_message_id:
            continue
        if since_ts is not None and int(m.get("time") or 0) < since_ts:
            continue
        sender = m.get("sender") or {}
        if str(sender.get("user_id")) == str(self_id):
            continue
        text = segments_text(m.get("message") or m.get("raw_message"))
        if not text or text.lstrip().startswith("/"):
            continue
        who = sender.get("card") or sender.get("nickname") or str(sender.get("user_id") or "群成员")
        lines.append(f"{who}：{text[:100]}")
    return lines


async def fetch_group_activity(
    bot: Bot,
    group_id: int,
    self_id,
    count: int,
    since_ts: int | None = None,
) -> dict[int, dict] | None:
    """拉群最近消息统计每人的发言条数（水群排行用）；接口失败返回 None。

    口径与路过统计（memory.record_seen）一致：不算 bot 自己、不算 / 命令，
    其余消息（含纯媒体）都计入；同一个人的昵称取最近一条消息里的。
    """
    try:
        resp = await bot.call_api("get_group_msg_history", group_id=group_id, count=count)
    except Exception as e:
        logger.warning("拉取群 %s 历史消息失败: %s", group_id, e)
        return None
    tally: dict[int, dict] = {}
    for m in resp.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if since_ts is not None and int(m.get("time") or 0) < since_ts:
            continue
        sender = m.get("sender") or {}
        uid = sender.get("user_id")
        if uid is None or str(uid) == str(self_id):
            continue
        text = segments_text(m.get("message") or m.get("raw_message"))
        if text.lstrip().startswith("/"):
            continue
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        entry = tally.setdefault(uid, {"nickname": "", "msgs": 0})
        entry["nickname"] = (
            sender.get("card") or sender.get("nickname") or entry["nickname"] or str(uid)
        )
        entry["msgs"] += 1
    return tally
