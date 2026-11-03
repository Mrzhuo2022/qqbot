"""会话历史压缩（滚动摘要）测试：阈值触发、失败回退、开关。"""

import asyncio

import src.shared.llm as llm
from src.shared.config import plugin_config


def _msgs(n: int) -> list[dict]:
    """构造 n 条交替消息。"""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


def test_shrink_below_threshold_noop(monkeypatch):
    monkeypatch.setattr(plugin_config, "llm_max_context_rounds", 8)
    history = _msgs(10)  # 10 <= 8*2
    out = asyncio.run(llm._shrink_history(history, ""))
    assert len(history) == 10
    assert out == ""


def test_shrink_compresses_into_summary(monkeypatch):
    monkeypatch.setattr(plugin_config, "llm_max_context_rounds", 8)
    monkeypatch.setattr(plugin_config, "llm_history_compress", True)
    monkeypatch.setattr(plugin_config, "llm_compress_keep_rounds", 3)

    async def fake_chat(system, prompt, **kwargs):
        assert "摘要" in system
        return "旧对话的合并摘要"

    monkeypatch.setattr(llm, "llm_chat", fake_chat)

    history = _msgs(20)  # > 16 触发压缩
    summary = asyncio.run(llm._shrink_history(history, "更早的摘要"))
    assert summary == "旧对话的合并摘要"
    assert len(history) == 6  # keep_rounds*2 条原文
    assert history[-1]["content"] == "m19"
    assert history[0]["content"] == "m14"


def test_shrink_falls_back_to_truncate_on_error(monkeypatch):
    """摘要 LLM 挂了就退回截断，不能卡回复。"""
    monkeypatch.setattr(plugin_config, "llm_max_context_rounds", 8)
    monkeypatch.setattr(plugin_config, "llm_history_compress", True)
    monkeypatch.setattr(plugin_config, "llm_compress_keep_rounds", 3)

    async def boom(*a, **kw):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(llm, "llm_chat", boom)

    history = _msgs(20)
    summary = asyncio.run(llm._shrink_history(history, "原有摘要"))
    assert summary == "原有摘要"
    assert len(history) == 16  # max_len 截断
    assert history[0]["content"] == "m4"


def test_shrink_falls_back_on_summary_timeout(monkeypatch):
    """摘要超过 30 秒硬上限同样退回截断。"""
    monkeypatch.setattr(plugin_config, "llm_max_context_rounds", 8)
    monkeypatch.setattr(plugin_config, "llm_history_compress", True)
    monkeypatch.setattr(plugin_config, "llm_compress_keep_rounds", 3)
    monkeypatch.setattr(llm, "_SUMMARIZE_TIMEOUT", 0.01)

    async def slow(*a, **kw):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(llm, "llm_chat", slow)

    history = _msgs(20)
    summary = asyncio.run(llm._shrink_history(history, ""))
    assert summary == ""
    assert len(history) == 16


def test_shrink_disabled_truncates(monkeypatch):
    monkeypatch.setattr(plugin_config, "llm_max_context_rounds", 8)
    monkeypatch.setattr(plugin_config, "llm_history_compress", False)

    history = _msgs(20)
    summary = asyncio.run(llm._shrink_history(history, ""))
    assert len(history) == 16
    assert summary == ""
