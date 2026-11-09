"""TTS 语音合成共享模块测试：配置门控、朗读文本净化、合成参数与回退、群级开关。"""

import asyncio
from types import SimpleNamespace

import httpx
from openai import BadRequestError

from src.shared import storage, tts
from src.shared.config import plugin_config


def _cfg(monkeypatch, **kw):
    monkeypatch.setattr(plugin_config, "tts_api_url", kw.get("url", ""))
    monkeypatch.setattr(plugin_config, "tts_api_key", kw.get("key", ""))
    monkeypatch.setattr(plugin_config, "tts_model", kw.get("model", ""))
    monkeypatch.setattr(plugin_config, "tts_voice", kw.get("voice", "alloy"))
    monkeypatch.setattr(plugin_config, "tts_speed", kw.get("speed", 1.0))
    monkeypatch.setattr(plugin_config, "tts_max_chars", kw.get("max_chars", 150))


class _FakeApi:
    """client.audio.speech 的替身。"""

    def __init__(self, fn):
        self._fn = fn

    async def create(self, **kwargs):
        return self._fn(**kwargs)


class _FakeClient:
    def __init__(self, fn):
        self.audio = SimpleNamespace(speech=_FakeApi(fn))


def _bad_request_error():
    req = httpx.Request("POST", "https://api.example.com/v1/audio/speech")
    resp = httpx.Response(400, request=req, json={"error": {"message": "unknown param"}})
    return BadRequestError("unknown param", response=resp, body=None)


def test_ready_requires_model(monkeypatch):
    _cfg(monkeypatch, model="")
    assert not tts.tts_ready()
    _cfg(monkeypatch, model="test-tts")
    assert tts.tts_ready()  # api_url 留空走 OpenAI 官方


def test_speech_text_cleans(monkeypatch):
    _cfg(monkeypatch, max_chars=150)
    assert tts.speech_text("看这个 https://example.com/a?b=1 好玩") == "看这个 好玩"
    assert tts.speech_text("你好[CQ:at,qq=all]呀") == "你好呀"
    assert tts.speech_text("a\n\nb   c") == "a b c"
    assert tts.speech_text("x" * 200) == "x" * 150


def test_speech_text_stops_url_at_cjk(monkeypatch):
    """URL 后紧跟中文标点/汉字时不能整句吞掉（对齐 utils.URL_RE 的排除集）。"""
    _cfg(monkeypatch)
    assert tts.speech_text("详情https://example.com/a。好的") == "详情。好的"


def test_send_reply_voice_branch(monkeypatch, tmp_path):
    """send_reply 语音分支：短回复纯语音；长产出语音+完整文字照发；发送失败回落文字。"""
    import asyncio
    from types import SimpleNamespace

    from src.shared import storage, utils
    from src.shared.utils import send_reply

    class _R:
        content = b"WAV"

    async def fake_synthesize(_text):
        return _R()

    monkeypatch.setattr(
        storage, "_settings_store", storage.JsonStore(tmp_path / "s.json")
    )
    _cfg(monkeypatch, model="test-tts")
    monkeypatch.setattr(plugin_config, "group_plugin_off", {})
    monkeypatch.setattr(utils.tts, "synthesize", fake_synthesize)

    event = SimpleNamespace(message_id=9, group_id=100)
    sent = []

    class FakeMatcher:
        async def send(self, msg):
            sent.append(str(msg))

    # 短回复：只发一条裸语音（语音挂不了引用段），不发文字
    asyncio.run(send_reply(FakeMatcher(), event, "你好呀", 1000))
    assert len(sent) == 1 and "record" in sent[0] and "[CQ:reply" not in sent[0]

    # 长产出（> tts_max_chars）：一条裸语音 + 完整文字照发
    long_text = "标题\n" + "内容" * 100
    sent.clear()
    asyncio.run(send_reply(FakeMatcher(), event, long_text, 100000, quote=False))
    assert len(sent) == 2 and "record" in sent[0] and sent[1].startswith("标题")

    # 语音发不出去（协议端不支持）：回落完整文字
    class FailOnRecord:
        async def send(self, msg):
            if "record" in str(msg):
                raise RuntimeError("协议端发不了语音")
            sent.append(str(msg))

    sent.clear()
    asyncio.run(send_reply(FailOnRecord(), event, "你好呀", 1000))
    assert len(sent) == 1 and "你好呀" in sent[0]


def test_synthesize_success(monkeypatch):
    _cfg(monkeypatch, model="test-tts", voice="nova", speed=1.2)
    captured = {}

    def _fn(**kwargs):
        captured.update(kwargs)

        class R:
            content = b"WAV-BYTES"
        return R()

    monkeypatch.setattr(tts, "_client", lambda *_a: _FakeClient(_fn))
    assert asyncio.run(tts.synthesize("你好")) == b"WAV-BYTES"
    assert captured["model"] == "test-tts"
    assert captured["input"] == "你好"
    assert captured["voice"] == "nova"
    assert captured["response_format"] == "wav"
    assert captured["speed"] == 1.2


def test_synthesize_retries_without_optional_params(monkeypatch):
    """兼容网关拒绝扩展参数时，去掉 speed/response_format 用最小参数重试。"""
    _cfg(monkeypatch, model="test-tts")
    calls = []

    def _fn(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise _bad_request_error()

        class R:
            content = b"ok"
        return R()

    monkeypatch.setattr(tts, "_client", lambda *_a: _FakeClient(_fn))
    assert asyncio.run(tts.synthesize("你好")) == b"ok"
    assert len(calls) == 2
    assert "response_format" not in calls[1] and "speed" not in calls[1]


def test_synthesize_failure_returns_none(monkeypatch):
    """服务挂了/超时返回 None，由调用方回落文字，不往外抛异常。"""
    _cfg(monkeypatch, model="test-tts")

    def _fn(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tts, "_client", lambda *_a: _FakeClient(_fn))
    assert asyncio.run(tts.synthesize("你好")) is None


def test_pad_silence_adds_head_tail():
    """首尾垫静音：时长增加、格式参数不变、头部确实是静音。"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x02" * 2400)  # 0.1s 有声数据
    padded = tts._pad_silence(buf.getvalue())

    with wave.open(io.BytesIO(padded)) as r:
        assert r.getnframes() == 2400 + int(24000 * tts.PAD_HEAD_S) + int(
            24000 * tts.PAD_TAIL_S
        )
        assert (r.getframerate(), r.getnchannels(), r.getsampwidth()) == (24000, 1, 2)
        head = r.readframes(int(24000 * tts.PAD_HEAD_S))
        assert head == b"\x00" * len(head)


def test_pad_silence_passthrough_non_wav():
    """非 WAV 数据原样返回，不做尽力而为之外的处理。"""
    assert tts._pad_silence(b"not a wav") == b"not a wav"


def test_voice_reply_on_gated(monkeypatch, tmp_path):
    """群级开关走 /bot off|on 体系：手动覆盖 > .env 的 group_plugin_off > 默认开。"""
    monkeypatch.setattr(
        storage, "_settings_store", storage.JsonStore(tmp_path / "s.json")
    )
    _cfg(monkeypatch, model="test-tts")
    monkeypatch.setattr(plugin_config, "group_plugin_off", {})

    # 服务就绪、没手动设置 → 默认开
    assert tts.voice_reply_on(100) is True
    # /bot off 语音 → 手动关，别的群不受影响
    storage.set_group_plugin_id(100, "voice", False)
    assert tts.voice_reply_on(100) is False
    assert tts.voice_reply_on(200) is True

    # .env 群级默认关闭（没手动设置过的群）
    monkeypatch.setattr(plugin_config, "group_plugin_off", {300: ["voice"]})
    assert tts.voice_reply_on(300) is False

    # 服务没配置 → 全关
    _cfg(monkeypatch, model="")
    assert tts.voice_reply_on(200) is False
