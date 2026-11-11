"""简单的 JSON 持久化：定时任务去重标记、提醒等运行时状态，重启不丢。

数据落在项目根的 data/ 目录，原子写入（临时文件 + replace）。
聊天记录类的高频数据不用这里（见 chat_store.py 的 SQLite 存储），
提醒/去重标记这类低频小数据走 JsonStore 足够。
群级功能开关：.env 的 group_plugin_off 是默认状态，/bot off|on 写入的
运行时覆盖（settings.json）优先于它——改开关不用动 .env 重启。
"""

import json
import logging
from pathlib import Path

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from src.shared.config import plugin_config

logger = logging.getLogger("storage")

DATA_DIR = Path("data")


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                logger.exception("状态文件损坏，重新创建：%s", self.path)
                self._data = {}
        else:
            self._data = {}

    @property
    def data(self) -> dict:
        return self._data

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    def remove(self, key: str) -> None:
        self._data.pop(key, None)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)


_settings_store = JsonStore(DATA_DIR / "settings.json")


# ── 会话 key：群聊/私聊的隔离标识 ───────────────────────

def session_key_str(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"g:{event.group_id}:{event.user_id}"
    return f"p:{event.user_id}:{event.user_id}"


# ── 通用设置（定时任务去重标记等）────────────────────────

def get_setting(key: str, default=None):
    return _settings_store.get(key, default)


def set_setting(key: str, value) -> None:
    _settings_store.set(key, value)


# ── 群级功能开关（.env 默认 + /bot off|on 运行时覆盖）────

# 所有支持群级开关的插件名（group_plugin_off 的合法值也来自这里）
PLUGINS = (
    "chat", "link_parser", "video_parser", "forward_parser",
    "chat_analyze", "quote", "news", "ainews", "hndaily", "search",
    "group_digest", "remind", "rank", "weather", "translate", "image_gen",
    "proactive", "voice",
)


def group_plugin_enabled_id(group_id: int, plugin: str) -> bool:
    """/bot off|on 的手动开关优先；没有手动设置时看 .env 的 group_plugin_off。"""
    override = group_plugin_override_id(group_id, plugin)
    if override is not None:
        return override
    return plugin not in plugin_config.group_plugin_off.get(group_id, [])


def group_plugin_enabled(event: GroupMessageEvent, plugin: str) -> bool:
    return group_plugin_enabled_id(event.group_id, plugin)


def group_plugin_override_id(group_id: int, plugin: str) -> bool | None:
    """运行时手动开关（/bot off|on 写入 settings.json）；None = 跟随默认。"""
    data = _settings_store.get(f"gplugin:{group_id}")
    if isinstance(data, dict) and plugin in data:
        return bool(data[plugin])
    return None


def set_group_plugin_id(group_id: int, plugin: str, enabled: bool) -> None:
    """/bot off|on 的落点；该群其他插件的手动开关不受影响。"""
    key = f"gplugin:{group_id}"
    data = _settings_store.get(key)
    data = dict(data) if isinstance(data, dict) else {}
    data[plugin] = enabled
    _settings_store.set(key, data)


def legacy_group_offs() -> dict[int, list[str]]:
    """旧版 /bot off 命令写进 settings.json 的开关（off:{群号}），迁移提示用。"""
    return {
        int(key[3:]): value
        for key, value in _settings_store.data.items()
        if key.startswith("off:") and isinstance(value, list) and value
    }
