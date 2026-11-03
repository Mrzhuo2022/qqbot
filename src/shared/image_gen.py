"""图像生成：通用 OpenAI images API（POST {base_url}/images/generations）。

任何兼容该格式的服务都能接（OpenAI dall-e / 智谱 cogview / SiliconFlow
Kolors / 本地部署的 OpenAI 格式网关等）。提示词只发给操作者自己配置的服务；
响应里若给的是图片 URL，下载仍走 safe_fetch 防 SSRF——仅当 URL 主机与
image_gen_base_url 同主机（比如服务就是本机部署的）才允许直连。
"""

import asyncio
import base64
import logging
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI, BadRequestError

from src.shared.config import plugin_config
from src.shared.fetch import UnsafeURL, safe_fetch

logger = logging.getLogger("image_gen")

_client_instance: AsyncOpenAI | None = None


def image_gen_ready() -> bool:
    return bool(plugin_config.image_gen_api_key and plugin_config.image_gen_model)


def _timeout() -> float:
    # 防止把 image_gen_timeout 配成 0/负数后请求瞬间超时
    return max(float(plugin_config.image_gen_timeout), 30.0)


def _client(timeout: float) -> AsyncOpenAI:
    global _client_instance
    if _client_instance is None:
        # max_retries=0：慢端点不做 SDK 级静默重试（会成倍拉长等待），
        # 超时快速失败，由调用方给明确提示
        _client_instance = AsyncOpenAI(
            api_key=plugin_config.image_gen_api_key,
            base_url=plugin_config.image_gen_base_url or None,
            timeout=timeout,
            max_retries=0,
        )
    return _client_instance


async def generate(prompt: str) -> bytes:
    """按提示词生成一张图，返回图片字节。失败抛异常，由调用方兜文案。"""
    if not image_gen_ready():
        raise RuntimeError(
            "图像生成未配置：请在 .env 中填写 image_gen_api_key 和 image_gen_model"
        )
    timeout = _timeout()
    client = _client(timeout)
    try:
        resp = await asyncio.wait_for(
            client.images.generate(
                model=plugin_config.image_gen_model,
                prompt=prompt,
                n=1,
                size=plugin_config.image_gen_size or None,
                response_format="b64_json",
            ),
            timeout=timeout,
        )
    except BadRequestError:
        # 有些兼容网关不认 response_format/size 这类扩展参数；去掉重试一次，
        # 让返回格式交给服务默认（可能给 URL，下面两种都处理）
        logger.info("图像接口拒绝了扩展参数，改用最小参数重试")
        resp = await asyncio.wait_for(
            client.images.generate(
                model=plugin_config.image_gen_model, prompt=prompt, n=1
            ),
            timeout=timeout,
        )

    items = resp.data or []
    if not items:
        raise RuntimeError("图像接口没有返回图片")
    first = items[0]
    if first.b64_json:
        return base64.b64decode(first.b64_json)
    if first.url:
        return await _download(str(first.url))
    raise RuntimeError("图像接口返回了无法识别的数据格式")


async def _download(url: str) -> bytes:
    try:
        resp = await safe_fetch(url)
        return resp.content
    except UnsafeURL:
        # URL 来自操作者自己配置的图像服务；同主机说明可能是本机部署
        # （127.0.0.1/内网网段），safe_fetch 会拦，这里放行直连。
        # 不同主机仍拒绝，防止服务被诱导返回内网探针 URL。
        url_host = urlparse(url).hostname
        base_host = urlparse(plugin_config.image_gen_base_url).hostname
        if url_host and url_host == base_host:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content
        raise
