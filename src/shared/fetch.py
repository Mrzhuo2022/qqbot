"""带 SSRF 防护的安全抓取。

群友可以发任意链接让 bot 去抓，若不设防，内网服务（127.0.0.1、
192.168.x.x、云元数据 169.254.169.254 等）都会被探测且内容经总结回显到群里。
规则：只允许 http(s) + 解析后全部 IP 都是公网地址；重定向手动逐跳跟随，
每一跳都重新校验；响应体流式读取并限制体积，超大文件中途掐断。

已知限制：校验与实际请求是两次 DNS 解析，理论上存在 DNS rebinding 窗口，
对个人群 bot 的威胁模型足够，不为此引入 IP 钉扎。
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from src.shared.config import plugin_config

logger = logging.getLogger("fetch")


class UnsafeURL(Exception):
    """目标不是可抓取的公网地址。"""


class PayloadTooLarge(Exception):
    """响应体超过 max_bytes 上限，已在下载中途掐断。"""


def ip_allowed(raw: str) -> bool:
    """IP 字面量（v4/v6，含 scope id 形式）是否为可访问的公网地址。"""
    try:
        ip = ipaddress.ip_address(raw.split("%")[0])
    except ValueError:
        return False
    # IPv4-mapped IPv6（::ffff:1.2.3.4）按内嵌的 IPv4 判定，防伪装绕过
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # 放行网段优先：本机代理（Clash 等 fake-IP 模式）会把所有域名解析成
    # 198.18.0.0/15，由代理接管并转发，不是真实内网
    for net in plugin_config.ssrf_allow_nets:
        try:
            if ip in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_allowed(host: str) -> bool:
    """主机名（或 IP 字面量）解析出的所有 IP 是否全为公网地址。"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    ips = {info[4][0] for info in infos}
    return bool(ips) and all(ip_allowed(ip) for ip in ips)


def url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return host_allowed(parsed.hostname)


async def safe_fetch(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 20.0,
    max_redirects: int = 3,
    max_bytes: int | None = None,
    read_body: bool = True,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """抓取 URL，每一跳（含重定向目标）都校验公网地址，违规则抛 UnsafeURL。

    响应体流式读取，超过 max_bytes（默认 fetch_max_mb）中途掐断并抛
    PayloadTooLarge，防止把超大文件整个读进内存。
    read_body=False 时不读正文、不解码（响应体为空，仅供取 .url/.headers）：
    给只要「跳转后最终 URL」的调用方用，免疫服务端的压缩编码问题。
    """
    if max_bytes is None:
        max_bytes = plugin_config.fetch_max_mb * 1024 * 1024
    # 声明不收压缩编码：部分 CDN（B站/微信公众号等）谎报 Content-Encoding，
    # httpx 自动解压时直接炸 zlib 错误。图片等二进制内容本来就不压缩，无副作用
    merged_headers = dict(headers or {})
    merged_headers.setdefault("Accept-Encoding", "identity")
    # 凭据只在原始域使用：跨域重定向剥离 Cookie/Authorization/Referer，
    # 防止带站 A 的登录态去访问重定向后的站 B
    origin_host = urlparse(url).hostname
    # raw=True 表示服务器谎报编码、正常解压失败过一次，改拿未解压的原始字节
    use_raw = False
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=_transport
    ) as client:
        for _ in range(max_redirects + 1):
            # DNS 解析是阻塞的，丢线程池
            if not await asyncio.to_thread(url_allowed, url):
                raise UnsafeURL(f"拒绝抓取非公网地址：{url[:80]}")
            hop_headers = merged_headers
            if urlparse(url).hostname != origin_host:
                hop_headers = {
                    k: v
                    for k, v in merged_headers.items()
                    if k.lower() not in ("cookie", "authorization", "referer")
                }
            async with client.stream("GET", url, headers=hop_headers) as resp:
                if resp.is_redirect and resp.has_redirect_location:
                    location = resp.headers.get("location", "")
                    url = str(resp.url.join(location))
                    continue
                if not read_body:
                    return httpx.Response(
                        resp.status_code,
                        headers=_clean_transfer_headers(resp.headers),
                        content=b"",
                        request=resp.request,
                    )
                # Content-Length 是压缩后的体积，可提前拒绝；没有它靠流式累计兜底
                content_length = resp.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > max_bytes:
                    raise PayloadTooLarge(
                        f"响应体积 {int(content_length)} 字节，超过上限 {max_bytes}"
                    )
                buf = bytearray()
                try:
                    chunks = resp.aiter_raw() if use_raw else resp.aiter_bytes()
                    async for chunk in chunks:
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise PayloadTooLarge(
                                f"响应体超过 {max_bytes} 字节，已中断下载"
                            )
                except httpx.DecodingError:
                    # 服务器无视 Accept-Encoding 还谎报编码，正常解压走不通：
                    # 重发一次拿原始字节，内容按原样交给调用方（不再抛异常）
                    use_raw = True
                    continue
                # 流已消费，用公开 API 重建一个装好内容的响应，调用方照常
                # 使用 .text/.content/.headers/.url；内容已是最终字节，
                # 残留的压缩编码/长度头必须清掉，否则再次解压会炸
                return httpx.Response(
                    resp.status_code,
                    headers=_clean_transfer_headers(resp.headers),
                    content=bytes(buf),
                    request=resp.request,
                )
    raise UnsafeURL("重定向次数过多")


def _clean_transfer_headers(headers: httpx.Headers) -> httpx.Headers:
    """去掉解压相关的传输头：重建的响应体已是最终字节，这些头不再成立。"""
    cleaned = httpx.Headers(headers)
    cleaned.pop("content-encoding", None)
    cleaned.pop("content-length", None)
    return cleaned
