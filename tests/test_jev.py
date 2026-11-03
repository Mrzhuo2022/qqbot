"""Jev 题目组、state 构建、卡片渲染与客户端重试的测试。"""

import asyncio

import httpx
import pytest

from src.plugins.chat_analyze import questions as q
from src.shared import jev
from src.shared.config import plugin_config


@pytest.fixture(autouse=True)
def _isolate_jev_mode(monkeypatch):
    """真实 .env 可能配了 jev_local_url，测试默认回到「无配置」，
    要测哪种模式由各用例自己开，避免本机配置漏进测试。"""
    monkeypatch.setattr(plugin_config, "jev_local_url", "")
    monkeypatch.setattr(plugin_config, "jev_local_model", "")
    monkeypatch.setattr(plugin_config, "jev_api_key", "")


def test_single_question_set_structure():
    for name, item in q.SINGLE_QUESTIONS.items():
        assert item["type"] in ("noul", "choice", "score"), name
        assert item["instructions"].isascii(), name
        if item["type"] == "choice":
            assert all(v.isascii() for v in item["criteria"].values()), name
        if item["type"] == "score":
            assert 2 <= len(item["criteria"]) <= 10, name
            assert all(c.isascii() for c in item["criteria"]), name


def _fake_single_answer(risk=4.2, intent="venting_mocking", intent_p=0.61,
                        needs_action=0.2, check_history=0.9,
                        hold_substance=0.8, needs_reply=0.3):
    return {
        "answers": {
            "conflict_risk": {"type": "score", "score": risk, "confidence": 0.66},
            "true_intent": {"type": "choice", "choice": intent,
                            "probabilities": {intent: intent_p}, "confidence": 0.72},
            "needs_action": {"type": "noul", "noul": needs_action},
            "check_history": {"type": "noul", "noul": check_history},
            "hold_substance": {"type": "noul", "noul": hold_substance},
            "needs_reply": {"type": "noul", "noul": needs_reply},
        }
    }


def test_format_single_card_renders_rating_intent_advice():
    card = q.format_single_card(_fake_single_answer(), "老王", "呵呵，你接着说", 10)
    assert "来自 老王" in card
    assert "原话：呵呵，你接着说" in card
    assert "危险 4/9 · 朋友会介入" in card  # risk=4.2 → round 4 → 第 5 档
    assert "真实意图：发泄情绪/阴阳怪气（把握 61%）" in card
    assert "应对：翻聊天记录 · 先别给实质" in card  # needs_reply=0.3<0.5 → 还应有"可以不回"
    assert "可以不回" in card
    assert "结合最近 10 条" in card
    assert "候选回复" not in card  # 未传候选时不渲染该段


def test_format_single_card_with_ranked_replies():
    replies = [("正面回应", 0.55), ("接梗玩闹", 0.30), ("暂不表态", 0.15)]
    card = q.format_single_card(_fake_single_answer(), "老王", "呵呵", 5, replies)
    assert "候选回复（Jev 排序）" in card
    assert "1. 正面回应（55%）" in card
    assert "2. 接梗玩闹（30%）" in card
    assert "3. 暂不表态（15%）" in card


def test_format_single_card_low_risk_and_no_advice():
    # risk=0 → 🟢；三个标签全关、needs_reply=0.9 → 无标签，走兜底文案
    card = q.format_single_card(
        _fake_single_answer(risk=0.4, intent="casual_chat", needs_action=0.1,
                            check_history=0.1, hold_substance=0.1, needs_reply=0.9),
        "A", "在吗", 0,
    )
    assert "🟢 危险 0/9 · 无风险" in card
    assert "应对：正常接话就行" in card
    assert "缓冲里没有更早的上下文" in card


def test_format_single_card_no_text_shows_placeholder():
    card = q.format_single_card(_fake_single_answer(), "A", "", 5)
    assert "原话：（无文字）" in card


def test_build_rank_questions_and_rank():
    qs = q.build_rank_questions(["马上发你", "稍等，在开会", "啥文件？"])
    item = qs["best_reply"]
    assert item["type"] == "choice"
    assert item["instructions"].isascii()
    assert item["criteria"] == {"c1": "马上发你", "c2": "稍等，在开会", "c3": "啥文件？"}
    result = {"answers": {"best_reply": {
        "type": "choice", "choice": "c1",
        "probabilities": {"c1": 0.89, "c2": 0.09, "c3": 0.02},
    }}}
    ranked = q.rank_candidates(result, ["马上发你", "稍等，在开会", "啥文件？"])
    assert ranked == [("马上发你", 0.89), ("稍等，在开会", 0.09), ("啥文件？", 0.02)]


def test_rank_candidates_missing_probs_zero():
    result = {"answers": {"best_reply": {"type": "choice", "choice": "c2",
                                         "probabilities": {"c2": 0.5}}}}
    ranked = q.rank_candidates(result, ["a", "b", "c"])
    assert ranked == [("b", 0.5), ("a", 0.0), ("c", 0.0)]


def test_parse_candidates_variants():
    import src.plugins.chat_analyze as mod

    assert mod.parse_candidates('["马上发你","稍等哈","这就发"]') == ["马上发你", "稍等哈", "这就发"]
    assert mod.parse_candidates('```json\n["a","b","c"]\n```') == ["a", "b", "c"]
    assert mod.parse_candidates('好的：["a","b","c"] 以上') == ["a", "b", "c"]
    assert mod.parse_candidates('不会输出数组') == []
    assert mod.parse_candidates("") == []
    long = mod.parse_candidates('["' + "很" * 80 + '","b","c"]')
    assert len(long[0]) == 50  # 截断到 50 字
    assert len(mod.parse_candidates('["a","b","c","d"]')) == 3


def test_question_set_structure():
    for name, item in q.QUESTIONS.items():
        assert item["type"] in ("noul", "choice", "score"), name
        assert item["instructions"].strip(), name
        # 口径：题目措辞一律英文
        assert item["instructions"].isascii(), name
        if item["type"] == "choice":
            assert 1 < len(item["criteria"]) <= 255, name
            assert all(v.isascii() for v in item["criteria"].values()), name
        if item["type"] == "score":
            assert 2 <= len(item["criteria"]) <= 10, name
            assert all(isinstance(c, str) and c for c in item["criteria"]), name
            assert all(c.isascii() for c in item["criteria"]), name


def test_build_state_parses_lines_and_caps():
    lines = [f"成员{i}：消息{i}" for i in range(30)]
    state = q.build_state(lines, group_id=123, max_messages=20)
    msgs = state["chat"]["messages"]
    assert len(msgs) == 20
    assert msgs[-1] == {"from": "成员29", "text": "消息29"}
    assert state["chat"]["latest_from"] == "成员29"
    assert state["chat"]["group_id"] == "123"


def test_build_state_hard_cap_40():
    lines = [f"u{i}：m{i}" for i in range(60)]
    msgs = q.build_state(lines, 1, max_messages=99)["chat"]["messages"]
    assert len(msgs) == 40


def test_build_state_malformed_line():
    state = q.build_state(["没有冒号的行"], 1)
    assert state["chat"]["messages"] == [{"from": "member", "text": "没有冒号的行"}]


def _fake_answer(atmo=1.2, risk=2.5, topic="casual_banter", open_q=0.0,
                 speak=0.9, action="summarize_discussion", needs="entertainment",
                 wind=0.1):
    def score(s, conf=0.8):
        return {"type": "score", "score": s, "confidence": conf}

    def choice(k):
        return {"type": "choice", "choice": k, "confidence": 0.7}

    def noul(v):
        return {"type": "noul", "noul": v}

    return {
        "answers": {
            "group_atmosphere": score(atmo),
            "escalation_risk": score(risk),
            "dominant_topic": choice(topic),
            "open_question": noul(open_q),
            "should_bot_speak": noul(speak),
            "best_bot_action": choice(action),
            "group_needs": choice(needs),
            "wind_down": noul(wind),
        }
    }


def test_format_card_renders_fixed_labels():
    card = q.format_card(_fake_answer(), n_lines=20)
    assert "最近 20 条" in card
    assert "🟢 冲突 2/9 · 略有微词" in card  # risk=2.5 → round 2 → 第 3 档
    assert "气氛：轻松闲聊" in card
    assert "主题：闲聊斗嘴" in card
    assert "没人答的提问：无" in card
    assert "对话：还在进行" in card
    assert "若插话：总结当前讨论" in card  # speak=0.9 → 适合插话


def test_format_card_high_risk_and_no_speak():
    card = q.format_card(_fake_answer(risk=6.5, speak=0.1), n_lines=5)
    assert "🔴 冲突 6/9 · 互相指责" in card
    assert "此刻插话：不适合，先观望" in card


def test_format_card_requires_answers():
    with pytest.raises(RuntimeError):
        q.format_card({"foo": 1}, n_lines=5)


def test_ask_retries_on_429(monkeypatch):
    monkeypatch.setattr(jev, "_sleep", asyncio.sleep)  # 仍走真实 sleep 但下面清零
    monkeypatch.setattr(jev, "_sleep", lambda s: asyncio.sleep(0))
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "test-key")

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=_fake_answer())

    result = asyncio.run(
        jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
    )
    assert calls["n"] == 3
    assert "answers" in result


def test_ask_gives_up_after_3_tries(monkeypatch):
    monkeypatch.setattr(jev, "_sleep", lambda s: asyncio.sleep(0))
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "test-key")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(529)

    with pytest.raises(RuntimeError, match="限流"):
        asyncio.run(
            jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
        )
    assert calls["n"] == 3


def test_ask_401_readable(monkeypatch):
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "bad-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(RuntimeError, match="认证"):
        asyncio.run(
            jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
        )


def plugin_cfg():
    from src.shared.config import plugin_config

    return plugin_config


def _parse_count_via_plugin():
    from nonebot.adapters.onebot.v11 import Message

    import src.plugins.chat_analyze as mod

    def parse(arg: str):
        return mod._parse_count(Message(arg))

    return parse


def test_parse_count_variants():
    parse = _parse_count_via_plugin()
    assert parse("") == 1  # 无参数 → 单条分析
    assert parse("   ") == 1
    assert parse(" 5 ") == 5
    assert parse("5条") == 5
    assert parse("第5条") == 5
    assert parse("５") == 5  # 全角数字
    assert parse("5 6") == 5  # 只取第一个数字
    assert parse("1") == 1
    assert parse("99") == 40  # 钳到状态上限
    assert parse("0") == 1
    assert parse("abc") is None  # 解析不出数字 → 用法提示


def _plugin_mod():
    import src.plugins.chat_analyze as mod

    return mod


def test_segments_text_media_and_text():
    from src.shared import history

    segs = [
        {"type": "reply", "data": {"id": "1"}},
        {"type": "text", "data": {"text": "看这个 "}},
        {"type": "image", "data": {"url": "x"}},
    ]
    assert history.segments_text(segs) == "看这个 [图片]"
    assert history.segments_text([]) == ""
    assert history.segments_text(None) == ""
    # CQ 码字符串也能解析
    assert "图片" in history.segments_text("[CQ:image,file=abc]")


def test_fetch_history_filters_bot_and_commands():
    import asyncio

    mod = _plugin_mod()

    class FakeBot:
        async def call_api(self, api, **params):
            assert api == "get_group_msg_history"
            assert params["group_id"] == 10000
            assert params["count"] == 10  # want=5 → min(5*2, 60)，过滤余量翻倍
            return {"messages": [
                {"message_id": 1, "sender": {"user_id": 888888888, "card": "机器人"},
                 "message": [{"type": "text", "data": {"text": "我是bot自己发的"}}]},
                {"message_id": 2, "sender": {"user_id": 1, "card": "小明"},
                 "message": [{"type": "text", "data": {"text": "/news"}}]},
                {"message_id": 3, "sender": {"user_id": 1, "card": "小明"},
                 "message": [{"type": "text", "data": {"text": "今天天气不错"}},
                             {"type": "image", "data": {}}]},
                {"message_id": 4, "sender": {"user_id": 2, "nickname": "小红"},
                 "message": [{"type": "text", "data": {"text": "是啊"}}]},
                {"message_id": 999, "sender": {"user_id": 2, "nickname": "小红"},
                 "message": [{"type": "text", "data": {"text": "本次命令本身"}}]},
            ]}

    event = type("E", (), {"group_id": 10000, "self_id": 888888888, "message_id": 999})()
    lines = asyncio.run(mod._fetch_history(FakeBot(), event, 5))
    assert lines == ["小明：今天天气不错[图片]", "小红：是啊"]


def test_fetch_history_api_failure_returns_empty():
    import asyncio

    mod = _plugin_mod()

    class BadBot:
        async def call_api(self, api, **params):
            raise RuntimeError("api not supported")

    event = type("E", (), {"group_id": 1, "self_id": 2, "message_id": 3})()
    assert asyncio.run(mod._fetch_history(BadBot(), event, 5)) == []


def test_fetch_history_count_caps_at_60():
    import asyncio

    mod = _plugin_mod()
    seen = {}

    class RecBot:
        async def call_api(self, api, **params):
            seen["count"] = params["count"]
            return {"messages": []}

    event = type("E", (), {"group_id": 1, "self_id": 2, "message_id": 3})()
    assert asyncio.run(mod._fetch_history(RecBot(), event, 40)) == []
    assert seen["count"] == 60  # want=40 → min(80, 60)


def test_ask_deadline_caps_total_wait(monkeypatch):
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "test-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    with pytest.raises(RuntimeError, match="超时"):
        asyncio.run(
            jev.ask({"chat": {}}, q.QUESTIONS, deadline=0.2,
                    _transport=httpx.MockTransport(handler))
        )


# ── 本地后端（laya 模型推理服务，同 decisions 合同）──

LOCAL_URL = "http://127.0.0.1:8787/v1/systemone"


def test_jev_ready_local_without_key(monkeypatch):
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "")
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", "")
    assert not jev.jev_ready()
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", LOCAL_URL)
    assert jev.jev_ready()  # 本地后端不需要 key


def test_ask_local_mode_no_auth_no_model(monkeypatch):
    import json as json_mod

    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "")
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", LOCAL_URL)
    monkeypatch.setattr(plugin_cfg(), "jev_local_model", "")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json_mod.loads(request.content)
        return httpx.Response(200, json=_fake_single_answer())

    result = asyncio.run(
        jev.ask({"chat": {}}, q.SINGLE_QUESTIONS,
                _transport=httpx.MockTransport(handler))
    )
    assert seen["url"] == LOCAL_URL
    assert seen["auth"] is None  # 本地后端不带 Authorization
    assert "model" not in seen["body"]  # 不带 OpenRouter 模型名，用后端默认
    assert seen["body"]["state"] == {"chat": {}}
    assert "answers" in result


def test_ask_local_mode_with_model_name(monkeypatch):
    import json as json_mod

    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "")
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", LOCAL_URL)
    monkeypatch.setattr(plugin_cfg(), "jev_local_model", "typed")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_mod.loads(request.content)
        return httpx.Response(200, json=_fake_answer())

    asyncio.run(
        jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
    )
    assert seen["body"]["model"] == "typed"


def test_ask_local_backend_error_detail(monkeypatch):
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "")
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", LOCAL_URL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "inference failed: CUDA out of memory"})

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        asyncio.run(
            jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
        )


def test_ask_local_connect_error_readable(monkeypatch):
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "")  # 没 key 才不会回落 OpenRouter
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", LOCAL_URL)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RuntimeError, match="laya"):
        asyncio.run(
            jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
        )


def test_ask_local_falls_back_to_hosted(monkeypatch):
    """本地后端没启动 + 配了 key：自动回落 OpenRouter。"""
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", LOCAL_URL)
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "test-key")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")  # 本地后端没起
        return httpx.Response(200, json=_fake_answer())

    result = asyncio.run(
        jev.ask({"chat": {}}, q.QUESTIONS, _transport=httpx.MockTransport(handler))
    )
    assert "answers" in result
    assert len(calls) == 2
    assert calls[0].url.host == "127.0.0.1"  # 第一发打本地
    assert calls[1].url.host == "openrouter.ai"  # 回落到云端
    assert calls[1].headers.get("Authorization") == "Bearer test-key"


def test_ask_hosted_requires_key(monkeypatch):
    monkeypatch.setattr(plugin_cfg(), "jev_api_key", "")
    monkeypatch.setattr(plugin_cfg(), "jev_local_url", "")
    with pytest.raises(RuntimeError, match="未配置"):
        asyncio.run(jev.ask({"chat": {}}, q.QUESTIONS))


# ── 候选回复：生成/排序拆分后的单元 ──────────────────────

def test_gen_candidates_parses_llm_output(monkeypatch):
    from src.shared import llm

    monkeypatch.setattr(plugin_cfg(), "jev_suggest_replies", True)
    monkeypatch.setattr(llm, "llm_ready", lambda: True)

    async def fake_chat(system, prompt, **kw):
        assert "小红：是啊" in prompt  # 候选生成拿到的就是聊天行
        return '["接梗","正面回应","暂不表态"]'

    monkeypatch.setattr(llm, "llm_chat", fake_chat)
    cands = asyncio.run(_plugin_mod()._gen_candidates(["小明：在吗", "小红：是啊"]))
    assert cands == ["接梗", "正面回应", "暂不表态"]


def test_gen_candidates_llm_failure_returns_none(monkeypatch):
    from src.shared import llm

    monkeypatch.setattr(plugin_cfg(), "jev_suggest_replies", True)
    monkeypatch.setattr(llm, "llm_ready", lambda: True)

    async def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm, "llm_chat", boom)
    assert asyncio.run(_plugin_mod()._gen_candidates(["a：b"])) is None


def test_gen_candidates_disabled_skips_llm(monkeypatch):
    from src.shared import llm

    monkeypatch.setattr(plugin_cfg(), "jev_suggest_replies", False)
    called = {"n": 0}

    async def fake_chat(*a, **kw):
        called["n"] += 1
        return "[]"

    monkeypatch.setattr(llm, "llm_chat", fake_chat)
    assert asyncio.run(_plugin_mod()._gen_candidates(["a：b"])) is None
    assert called["n"] == 0


def test_rank_replies_sorts_and_swallows_failure(monkeypatch):
    from src.shared import jev as jev_mod

    async def fake_ask(state, questions, **kw):
        assert "best_reply" in questions
        return {"answers": {"best_reply": {
            "type": "choice", "choice": "c2",
            "probabilities": {"c1": 0.3, "c2": 0.6, "c3": 0.1}}}}

    monkeypatch.setattr(jev_mod, "ask", fake_ask)
    out = asyncio.run(_plugin_mod()._rank_replies(["a", "b", "c"], {"chat": {}}))
    assert out == [("b", 0.6), ("a", 0.3), ("c", 0.1)]

    async def boom(*a, **kw):
        raise RuntimeError("jev down")

    monkeypatch.setattr(jev_mod, "ask", boom)
    assert asyncio.run(_plugin_mod()._rank_replies(["a", "b"], {"chat": {}})) is None


def test_analyze_single_runs_ask_and_llm_concurrently(monkeypatch):
    """Jev 分析与 LLM 候选生成并发：互相等对方进场才放行，串行会超时失败。"""
    from src.shared import jev as jev_mod
    from src.shared import llm

    monkeypatch.setattr(plugin_cfg(), "jev_suggest_replies", True)
    monkeypatch.setattr(llm, "llm_ready", lambda: True)

    entered_ask = asyncio.Event()
    entered_llm = asyncio.Event()

    async def fake_ask(state, questions, **kw):
        if "conflict_risk" in questions:  # 单条分析题组
            entered_ask.set()
            await asyncio.wait_for(entered_llm.wait(), 2)
            return _fake_single_answer()
        return {"answers": {"best_reply": {  # 排序题组
            "type": "choice", "choice": "c1",
            "probabilities": {"c1": 0.7, "c2": 0.2, "c3": 0.1}}}}

    async def fake_chat(system, prompt, **kw):
        entered_llm.set()
        await asyncio.wait_for(entered_ask.wait(), 2)
        return '["r1","r2","r3"]'

    monkeypatch.setattr(jev_mod, "ask", fake_ask)
    monkeypatch.setattr(llm, "llm_chat", fake_chat)

    async def run():
        return await _plugin_mod()._analyze_single(["小明：在吗？", "小红：呵呵"], 10000)

    result, replies = asyncio.run(asyncio.wait_for(run(), 4))
    assert "answers" in result
    assert replies == [("r1", 0.7), ("r2", 0.2), ("r3", 0.1)]
