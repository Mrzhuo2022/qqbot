"""配置自检测试：缺关键配置时启动警告。"""

import logging

from src.shared import config as cfg


def test_log_config_warnings_flags_missing_keys(monkeypatch, caplog):
    monkeypatch.setattr(cfg.plugin_config, "onebot_access_token", "")
    monkeypatch.setattr(cfg.plugin_config, "llm_api_key", "")
    monkeypatch.setattr(cfg.plugin_config, "group_whitelist", [])
    with caplog.at_level(logging.INFO, logger="config"):
        cfg.log_config_warnings()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("onebot_access_token 为空" in m for m in msgs)
    assert any("llm_api_key 为空" in m for m in msgs)
    assert any("group_whitelist 为空" in m for m in msgs)


def test_log_config_warnings_quiet_when_configured(monkeypatch, caplog):
    monkeypatch.setattr(cfg.plugin_config, "onebot_access_token", "secret")
    monkeypatch.setattr(cfg.plugin_config, "llm_api_key", "sk-x")
    monkeypatch.setattr(cfg.plugin_config, "group_whitelist", [123])
    with caplog.at_level(logging.INFO, logger="config"):
        cfg.log_config_warnings()
    assert caplog.records == []
