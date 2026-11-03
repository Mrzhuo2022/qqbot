"""LLM 备用通道测试：通道选择、故障切换、探活回切。"""

import asyncio

import src.shared.chat_store as chat_store
import src.shared.llm as llm
from src.shared.config import plugin_config


def _setup(monkeypatch, main_key="k-main", fb_key="k-fb", fb_model="fb-model"):
    monkeypatch.setattr(plugin_config, "llm_api_key", main_key)
    monkeypatch.setattr(plugin_config, "llm_base_url", "https://main.test")
    monkeypatch.setattr(plugin_config, "llm_model", "main-model")
    monkeypatch.setattr(plugin_config, "llm_fallback_base_url", "https://fb.test")
    monkeypatch.setattr(plugin_config, "llm_fallback_api_key", fb_key)
    monkeypatch.setattr(plugin_config, "llm_fallback_model", fb_model)
    monkeypatch.setattr(plugin_config, "llm_vision_model", "vision-model")
    llm._main_failures = 0
    llm._request_seq = 0


def test_pick_order_basic(monkeypatch):
    _setup(monkeypatch)
    # 双方健康：主通道优先
    assert llm._pick_order(image=False) == ["main", "fallback"]
    # vision 不走备用
    assert llm._pick_order(image=True) == ["main"]
    # 主通道失败过：先走备用
    llm._main_failures = 1
    llm._request_seq = 1
    assert llm._pick_order(image=False) == ["fallback", "main"]


def test_pick_order_unconfigured(monkeypatch):
    _setup(monkeypatch, fb_key="", fb_model="")
    assert llm._pick_order(image=False) == ["main"]
    # 主备都没配：空列表，llm_chat 的 llm_ready() 会先拦下
    _setup(monkeypatch, main_key="", fb_key="", fb_model="")
    assert llm._pick_order(image=False) == []
    # 只有 fallback（主 key 留空）
    _setup(monkeypatch, main_key="")
    assert llm._pick_order(image=False) == ["fallback"]
    assert llm.llm_ready() is True


def test_pick_order_probe_cycle(monkeypatch):
    _setup(monkeypatch)
    llm._main_failures = 1
    # seq % 5 == 0 的请求探活主通道（还是主通道优先）
    for seq in (5, 10):
        llm._request_seq = seq
        assert llm._pick_order(image=False) == ["main", "fallback"], seq
    # 其余请求走备用优先
    for seq in (6, 7, 8, 9):
        llm._request_seq = seq
        assert llm._pick_order(image=False) == ["fallback", "main"], seq


def _fake_channels(monkeypatch, fail_main: bool, calls: list):
    """_channel/_call_channel 打桩：main-model 可失败，fallback-model 恒成功。"""

    async def fake_call(client, model, messages):
        calls.append(model)
        if model == "main-model" and fail_main:
            raise RuntimeError("主通道炸了")
        return f"来自 {model}"

    monkeypatch.setattr(llm, "_call_channel", fake_call)
    monkeypatch.setattr(llm, "_channel", lambda ch, img: (object(), f"{ch}-model"))


def test_fallback_switches_on_main_failure(monkeypatch, tmp_path):
    _setup(monkeypatch)
    chat_store.init(tmp_path / "chat.db")
    calls: list[str] = []
    _fake_channels(monkeypatch, fail_main=True, calls=calls)

    answer = asyncio.run(llm._chat_with_history("sys", "你好", "p:1:1", None))
    assert answer == "来自 fallback-model"
    assert calls == ["main-model", "fallback-model"]
    assert llm.main_healthy() is False
    assert llm.main_failures() == 1


def test_fallback_used_first_while_main_down(monkeypatch, tmp_path):
    """主通道挂过后，后续请求直接备用优先，不再每条都撞主通道。"""
    _setup(monkeypatch)
    chat_store.init(tmp_path / "chat.db")
    calls: list[str] = []
    _fake_channels(monkeypatch, fail_main=True, calls=calls)

    asyncio.run(llm._chat_with_history("sys", "第一条", "p:1:1", None))
    asyncio.run(llm._chat_with_history("sys", "第二条", "p:1:1", None))
    # 请求 1：main 失败→fallback；请求 2：备用先行成功，不再碰主通道
    assert calls == ["main-model", "fallback-model", "fallback-model"]
    assert llm.main_failures() == 1


def test_probe_recovers_main(monkeypatch, tmp_path):
    """探活请求主通道恢复 → 立即切回主通道。"""
    _setup(monkeypatch)
    chat_store.init(tmp_path / "chat.db")
    calls: list[str] = []
    _fake_channels(monkeypatch, fail_main=False, calls=calls)

    llm._main_failures = 2
    llm._request_seq = 4  # 下一条请求 seq=5，命中探活
    answer = asyncio.run(llm._chat_with_history("sys", "探活", "p:1:1", None))
    assert answer == "来自 main-model"
    assert llm.main_healthy() is True
    assert llm.main_failures() == 0


def test_all_channels_down_raises(monkeypatch, tmp_path):
    _setup(monkeypatch)
    chat_store.init(tmp_path / "chat.db")
    calls: list[str] = []

    async def always_fail(client, model, messages):
        calls.append(model)
        raise RuntimeError(f"{model} 挂了")

    monkeypatch.setattr(llm, "_call_channel", always_fail)
    monkeypatch.setattr(llm, "_channel", lambda ch, img: (object(), f"{ch}-model"))

    try:
        asyncio.run(llm._chat_with_history("sys", "你好", "p:1:1", None))
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert calls == ["main-model", "fallback-model"]


def test_image_request_skips_fallback(monkeypatch, tmp_path):
    """vision 请求失败就失败，不往纯文本备用通道上撞。"""
    _setup(monkeypatch)
    chat_store.init(tmp_path / "chat.db")
    calls: list[str] = []

    async def always_fail(client, model, messages):
        calls.append(model)
        raise RuntimeError("vision 挂了")

    monkeypatch.setattr(llm, "_call_channel", always_fail)
    monkeypatch.setattr(llm, "_channel", lambda ch, img: (object(), f"{ch}-model"))

    try:
        asyncio.run(llm._chat_with_history("sys", "看图", "p:1:1", "data:image/png;base64,x"))
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert calls == ["main-model"]  # 只试了主通道一次，没有备用
