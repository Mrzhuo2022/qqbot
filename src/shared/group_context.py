"""群聊上下文缓冲：给 chat 注入"最近群里在聊什么"。

放在共享层是因为高优先级插件（chat/video/forward，block=True）处理过的消息
不会再传播到 chat 插件里的收集器——恰恰这些消息最该进上下文，
所以各插件在处理时自行补写。

隔离与持久化：每群一个 deque，只进本群的注入与 /jev 分析，互不可见；
内存是热缓存，落盘走 chat_store（每群一行），重启后懒加载回来不丢。
"""

from collections import deque

from src.shared import chat_store
from src.shared.utils import sanitize_one_line

MAXLEN = 50
_buffers: dict[int, deque] = {}


def init(db_path=None) -> None:
    """绑定存储库并清空内存缓存；bot 启动与测试重置时调用。"""
    chat_store.init(db_path)
    _buffers.clear()


def _buffer(group_id: int) -> deque:
    buf = _buffers.get(group_id)
    if buf is None:
        buf = deque(chat_store.load_group_lines(group_id), maxlen=MAXLEN)
        _buffers[group_id] = buf
    return buf


def append(group_id: int, who: str, text: str) -> None:
    if not text:
        return
    # 这里是名片进提示词的收口：缓冲内容会原样拼进 chat/proactive 的
    # system 或上下文，入库前统一洗成单行，剥掉 CQ 码/控制字符/换行
    who = sanitize_one_line(who) or "某人"
    buf = _buffer(group_id)
    buf.append(f"{who}：{text[:100]}")
    chat_store.save_group_lines(group_id, list(buf))


def recent(group_id: int, n: int) -> list[str]:
    """最近 n 条记录，旧→新。"""
    if n <= 0:
        return []
    return list(_buffer(group_id))[-n:]


def clear(group_id: int) -> None:
    _buffers.pop(group_id, None)
    chat_store.delete_group_lines(group_id)
