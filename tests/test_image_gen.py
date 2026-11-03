"""图像生成共享模块测试：配置门控、b64/URL 解析、兼容参数回退、同主机直连。"""

import asyncio
import base64

import httpx
import pytest
from openai import BadRequestError
from openai.types.image import Image

from src.shared import image_gen
from src.shared.config import plugin_config


def _cfg(monkeypatch, **kw):
    monkeypatch.setattr(plugin_config, "image_gen_api_key", kw.get("key", "sk-test"))
    monkeypatch.setattr(plugin_config, "image_gen_model", kw.get("model", "test-model"))
    monkeypatch.setattr(plugin_config, "image_gen_base_url", kw.get("base", ""))
    monkeypatch.setattr(plugin_config, "image_gen_size", kw.get("size", "1024x1024"))


class _FakeResp:
    def __init__(self, items):
        self.data = items


def _bad_request_error():
    req = httpx.Request("POST", "https://api.example.com/v1/images/generations")
    resp = httpx.Response(400, request=req, json={"error": {"message": "unknown param"}})
    return BadRequestError("unknown param", response=resp, body=None)


def test_ready_requires_key_and_model(monkeypatch):
    _cfg(monkeypatch, key="", model="")
    assert not image_gen.image_gen_ready()
    _cfg(monkeypatch, key="sk-test", model="")
    assert not image_gen.image_gen_ready()
    _cfg(monkeypatch)
    assert image_gen.image_gen_ready()


def test_generate_b64(monkeypatch):
    _cfg(monkeypatch)
    raw = b"PNG-DATA"
    captured = {}

    class _FakeImages:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return _FakeResp([
                Image(b64_json=base64.b64encode(raw).decode())
            ])

    class _FakeClient:
        def __init__(self):
            self.images = _FakeImages()

    monkeypatch.setattr(image_gen, "_client", lambda *_a: _FakeClient())
    out = asyncio.run(image_gen.generate("一只猫"))
    assert out == raw
    assert captured["model"] == "test-model"
    assert captured["size"] == "1024x1024"
    assert captured["response_format"] == "b64_json"


def test_generate_retries_without_optional_params(monkeypatch):
    _cfg(monkeypatch)
    calls = []

    class _FakeImages:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise _bad_request_error()
            return _FakeResp([Image(b64_json=base64.b64encode(b"ok").decode())])

    class _FakeClient:
        def __init__(self):
            self.images = _FakeImages()

    monkeypatch.setattr(image_gen, "_client", lambda *_a: _FakeClient())
    out = asyncio.run(image_gen.generate("一只猫"))
    assert out == b"ok"
    assert len(calls) == 2
    assert "response_format" not in calls[1] and "size" not in calls[1]


def test_generate_url_download(monkeypatch):
    _cfg(monkeypatch)

    class _FakeImages:
        async def generate(self, **kwargs):
            return _FakeResp([Image(url="https://img.example.com/pic.png")])

    class _FakeClient:
        def __init__(self):
            self.images = _FakeImages()

    async def fake_fetch(url, **kw):
        assert url == "https://img.example.com/pic.png"

        class R:
            content = b"URL-BYTES"
        return R()

    monkeypatch.setattr(image_gen, "_client", lambda *_a: _FakeClient())
    monkeypatch.setattr(image_gen, "safe_fetch", fake_fetch)
    assert asyncio.run(image_gen.generate("一只猫")) == b"URL-BYTES"


def test_generate_url_rejects_cross_host_unsafe(monkeypatch):
    """服务返回的内网/异主机 URL：safe_fetch 拦下后不放行直连。"""
    _cfg(monkeypatch, base="https://api.example.com/v1")

    class _FakeImages:
        async def generate(self, **kwargs):
            return _FakeResp([Image(url="https://169.254.169.254/latest")])

    class _FakeClient:
        def __init__(self):
            self.images = _FakeImages()

    from src.shared.fetch import UnsafeURL

    async def fake_fetch(url, **kw):
        raise UnsafeURL("内网")

    monkeypatch.setattr(image_gen, "_client", lambda *_a: _FakeClient())
    monkeypatch.setattr(image_gen, "safe_fetch", fake_fetch)
    with pytest.raises(UnsafeURL):
        asyncio.run(image_gen.generate("一只猫"))


def test_generate_url_allows_same_host_direct(monkeypatch):
    """本机部署的图像服务返回同主机 URL（如 127.0.0.1）：放行直连。"""
    _cfg(monkeypatch, base="http://127.0.0.1:8787/v1")

    class _FakeImages:
        async def generate(self, **kwargs):
            return _FakeResp([Image(url="http://127.0.0.1:8787/img/1.png")])

    class _FakeClient:
        def __init__(self):
            self.images = _FakeImages()

    from src.shared.fetch import UnsafeURL

    async def fake_fetch(url, **kw):
        raise UnsafeURL("本机地址被 SSRF 防护拦截")

    class _FakeHttp:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url == "http://127.0.0.1:8787/img/1.png"

            class R:
                content = b"LOCAL-BYTES"

                def raise_for_status(self):
                    pass
            return R()

    monkeypatch.setattr(image_gen, "_client", lambda *_a: _FakeClient())
    monkeypatch.setattr(image_gen, "safe_fetch", fake_fetch)
    monkeypatch.setattr(image_gen.httpx, "AsyncClient", _FakeHttp)
    assert asyncio.run(image_gen.generate("一只猫")) == b"LOCAL-BYTES"


def test_generate_unconfigured_raises(monkeypatch):
    _cfg(monkeypatch, key="", model="")
    with pytest.raises(RuntimeError):
        asyncio.run(image_gen.generate("一只猫"))
