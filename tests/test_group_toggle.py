"""群级运行时开关（/bot off|on）与 admin 别名解析测试。"""

from pathlib import Path

from src.plugins.admin import PLUGIN_NAMES, _resolve_plugin
from src.shared import storage
from src.shared.config import plugin_config


def _isolated_store(tmp_path: Path, monkeypatch) -> None:
    """用临时 settings.json，避免测试写脏真实数据目录。"""
    monkeypatch.setattr(storage, "_settings_store", storage.JsonStore(tmp_path / "s.json"))


def test_override_takes_precedence_over_env(monkeypatch, tmp_path):
    """手动开关优先于 .env 的 group_plugin_off 默认值。"""
    _isolated_store(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_config, "group_plugin_off", {100: ["chat"]})

    # .env 默认：关
    assert storage.group_plugin_enabled_id(100, "chat") is False
    # /bot on 聊天 → 手动开，盖过 .env
    storage.set_group_plugin_id(100, "chat", True)
    assert storage.group_plugin_enabled_id(100, "chat") is True
    # 别的群不受影响
    assert storage.group_plugin_enabled_id(200, "chat") is True

    # 同群其他插件不受影响
    storage.set_group_plugin_id(100, "rank", False)
    assert storage.group_plugin_enabled_id(100, "chat") is True
    assert storage.group_plugin_enabled_id(100, "rank") is False


def test_override_none_means_env_default(monkeypatch, tmp_path):
    _isolated_store(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_config, "group_plugin_off", {100: ["chat"]})
    assert storage.group_plugin_override_id(100, "chat") is None
    assert storage.group_plugin_enabled_id(100, "chat") is False

    storage.set_group_plugin_id(100, "chat", True)
    assert storage.group_plugin_override_id(100, "chat") is True

    # .env 没关的群，默认就是开
    assert storage.group_plugin_enabled_id(300, "rank") is True


def test_resolve_plugin_aliases():
    assert _resolve_plugin("聊天") == "chat"
    assert _resolve_plugin("新闻") == "news"
    assert _resolve_plugin("晚报") == "group_digest"
    assert _resolve_plugin("水群") == "rank"
    assert _resolve_plugin("jev") == "chat_analyze"
    assert _resolve_plugin("chat") == "chat"  # 英文键直通
    assert _resolve_plugin("不存在的") is None
    assert _resolve_plugin("123") is None
    # 中文名表覆盖全部插件（/bot status 展示与 off|on 参数共用一张表）
    assert set(PLUGIN_NAMES) == set(storage.PLUGINS)
