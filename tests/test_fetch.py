"""SSRF 防护的地址判定测试（不依赖外网的纯函数部分）+ safe_fetch 流式行为。"""

import asyncio

import httpx
import pytest

from src.shared import fetch as fetch_mod
from src.shared.config import plugin_config
from src.shared.fetch import PayloadTooLarge, host_allowed, ip_allowed, safe_fetch, url_allowed


def test_ip_allowed_public():
    assert ip_allowed("8.8.8.8")
    assert ip_allowed("1.1.1.1")
    assert ip_allowed("2606:4700:4700::1111")


def test_ip_allowed_blocks_private_ranges():
    for bad in (
        "127.0.0.1",          # 环回
        "10.0.0.5",           # 内网 A
        "172.16.1.1",         # 内网 B
        "192.168.1.1",        # 内网 C
        "169.254.169.254",    # 云元数据 / 链路本地
        "0.0.0.0",            # 未指定
        "224.0.0.1",          # 组播
        "240.0.0.1",          # 保留
        "::1",                # v6 环回
        "fe80::1",            # v6 链路本地
        "fd00::1",            # v6 私有
        "fe80::1%eth0",       # 带 scope id
        "not-an-ip",
    ):
        assert not ip_allowed(bad), bad


def test_ip_allowed_blocks_ipv4_mapped_disguise():
    # ::ffff: 前缀包裹的内网地址不能因 v6 身份而漏过
    assert not ip_allowed("::ffff:127.0.0.1")
    assert not ip_allowed("::ffff:192.168.1.1")
    assert not ip_allowed("::ffff:169.254.169.254")
    assert ip_allowed("::ffff:8.8.8.8")


def test_ip_allowed_extra_nets_config(monkeypatch):
    monkeypatch.setattr(plugin_config, "ssrf_allow_nets", ["198.18.0.0/15"])
    assert ip_allowed("198.18.40.243")
    # 放行网段不能波及其他内网
    assert not ip_allowed("192.168.1.1")
    # 非法网段配置不炸，仅忽略
    monkeypatch.setattr(plugin_config, "ssrf_allow_nets", ["not-a-net"])
    assert not ip_allowed("198.18.40.243")


def test_host_allowed_ip_literal():
    assert host_allowed("8.8.8.8")
    assert not host_allowed("127.0.0.1")
    assert not host_allowed("192.168.1.100")


def test_host_allowed_localhost_resolves_to_loopback():
    # hosts 文件解析，不依赖外网
    assert not host_allowed("localhost")


def test_url_allowed_scheme_and_host():
    assert not url_allowed("ftp://example.com/x")
    assert not url_allowed("http://")
    assert not url_allowed("notaurl")
    # 内网字面量直接拒绝
    assert not url_allowed("http://127.0.0.1:8080/onebot/v11/ws")
    assert not url_allowed("http://169.254.169.254/latest/meta-data/")
    assert not url_allowed("http://[::1]:8080/")


# ── safe_fetch：流式读取、体积上限、重定向 ────────────────


@pytest.fixture
def allow_all_urls(monkeypatch):
    monkeypatch.setattr(fetch_mod, "url_allowed", lambda url: True)


def test_safe_fetch_roundtrip(allow_all_urls):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content="你好".encode()
        )

    resp = asyncio.run(safe_fetch("https://example.com/page", _transport=httpx.MockTransport(handler)))
    assert resp.status_code == 200
    assert resp.text == "你好"
    assert "text/html" in resp.headers["content-type"]
    assert str(resp.url) == "https://example.com/page"


def test_safe_fetch_rejects_oversize_content_length(allow_all_urls):
    # Content-Length 超限：连 body 都不用下就拒绝
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": str(10 * 1024 * 1024)}, content=b"tiny"
        )

    with pytest.raises(PayloadTooLarge):
        asyncio.run(
            safe_fetch(
                "https://example.com/big",
                max_bytes=1024,
                _transport=httpx.MockTransport(handler),
            )
        )


def test_safe_fetch_cuts_off_streamed_body(allow_all_urls):
    # 没有 Content-Length 时靠流式累计兜底，超限中途掐断
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10_000)

    with pytest.raises(PayloadTooLarge):
        asyncio.run(
            safe_fetch(
                "https://example.com/stream",
                max_bytes=1024,
                _transport=httpx.MockTransport(handler),
            )
        )


def test_safe_fetch_follows_redirect(allow_all_urls):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/short":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        return httpx.Response(200, content=b"landing")

    resp = asyncio.run(safe_fetch("https://example.com/short", _transport=httpx.MockTransport(handler)))
    assert resp.status_code == 200
    assert resp.text == "landing"
    assert str(resp.url) == "https://example.com/final"


def test_safe_fetch_default_limit_from_config(allow_all_urls, monkeypatch):
    monkeypatch.setattr(plugin_config, "fetch_max_mb", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024))

    with pytest.raises(PayloadTooLarge):
        asyncio.run(
            safe_fetch("https://example.com/mb", _transport=httpx.MockTransport(handler))
        )


def test_safe_fetch_read_body_false_skips_content(allow_all_urls):
    """read_body=False：正文不读不解码（坏压缩数据也带不动），仍拿到最终 URL。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        # 异步流式响应，模拟「声称 gzip 实际是坏数据」的服务端
        async def bad_body():
            yield b"\x1f\x8bgarbage"

        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=bad_body())

    transport = httpx.MockTransport(handler)
    resp = asyncio.run(
        safe_fetch(
            "https://example.com/redirect-target",
            read_body=False,
            _transport=transport,
        )
    )
    assert resp.status_code == 200
    assert resp.content == b""  # 正文被跳过
    assert str(resp.url) == "https://example.com/redirect-target"

    # 对照：默认 read_body=True 读到这段坏压缩数据直接炸
    with pytest.raises(httpx.DecodingError):
        asyncio.run(
            safe_fetch("https://example.com/redirect-target", _transport=transport)
        )


def test_safe_fetch_read_body_false_still_validates_redirects(monkeypatch):
    """read_body=False 的重定向逐跳校验照常：跳到内网一样拒绝。"""
    from src.shared.fetch import UnsafeURL

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/x"})

    # 第一跳放行，仅 127.0.0.1 走拒绝路径；断言错误信息证明是第二跳拒的
    # 内网目标，而不是第一跳 DNS 失败碰巧抛错（离线环境也能测准）
    monkeypatch.setattr(fetch_mod, "url_allowed", lambda u: "127.0.0.1" not in u)
    with pytest.raises(UnsafeURL) as ei:
        asyncio.run(
            safe_fetch(
                "https://example.com/jump",
                read_body=False,
                max_redirects=2,
                _transport=httpx.MockTransport(handler),
            )
        )
    assert "127.0.0.1" in str(ei.value)
