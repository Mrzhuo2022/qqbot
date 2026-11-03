"""Jev 判断模型客户端（TypeSafe jev：本地 laya 模型后端 或 OpenRouter decisions 接口）。

Jev 只回答选择题/打分/是非（noul/choice/score），不生成文字——输出是受控选项，
因此不存在自由文本注入回群的问题。协议要点（实测有效，别改）：
- POST {model?, state, questions}；questions 的 instructions/criteria 用英文，
  state 里的聊天内容保留中文原文
- jev_local_url 配置时优先走本地后端（开源的 laya 判断模型
  https://huggingface.co/convaiinnovations/laya，同一份 decisions 合同、免 key；
  model 字段只在配置了 jev_local_model 时携带）；
  本地连不上且配了 jev_api_key 时自动回落 OpenRouter，两头都通
- OpenRouter 429/529 需指数退避重试（最多 3 次）；401 key 错、422 body 不合法
- 重试连同退避受 deadline 总时限约束，最坏也就这么久，调用方不用干等
"""

import asyncio
import logging

import httpx

from src.shared.config import plugin_config

logger = logging.getLogger("jev")

RETRY_STATUS = (429, 529)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def jev_ready() -> bool:
    return bool(plugin_config.jev_api_key or plugin_config.jev_local_url)


def _local_parts(state: dict, questions: dict) -> tuple[str, dict, dict]:
    body: dict = {"state": state, "questions": questions}
    if plugin_config.jev_local_model:
        # 本地后端按注册名校验 model，OpenRouter 的模型名会 400
        body["model"] = plugin_config.jev_local_model
    return plugin_config.jev_local_url, {"Content-Type": "application/json"}, body


def _hosted_parts(state: dict, questions: dict) -> tuple[str, dict, dict]:
    if not plugin_config.jev_api_key:
        raise RuntimeError("未配置 Jev：填 jev_local_url（本地后端）或 jev_api_key（OpenRouter）")
    return (
        plugin_config.jev_api_url,
        {
            "Authorization": f"Bearer {plugin_config.jev_api_key}",
            "Content-Type": "application/json",
        },
        {"model": plugin_config.jev_model, "state": state, "questions": questions},
    )


async def _post_once(
    url: str,
    headers: dict,
    body: dict,
    timeout: float,
    deadline: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    try:
        return await asyncio.wait_for(
            _post_with_retries(url, body, headers, timeout, transport),
            timeout=deadline,
        )
    # asyncio.TimeoutError 在 3.10 上与内置 TimeoutError 不是同一个类，都要列出
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise RuntimeError("Jev 响应超时（含限流重试），稍后再试") from e


async def ask(
    state: dict,
    questions: dict,
    timeout: float = 20.0,
    deadline: float = 30.0,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """发起一次判断请求，返回原始 JSON 响应（含每道题的答案）。

    timeout 是单次尝试的时限，deadline 是含重试与退避的总时限——
    最坏 30 秒后报超时，而不是让调用方干等到重试耗尽。
    """
    local = bool(plugin_config.jev_local_url)
    url, headers, body = (
        _local_parts(state, questions) if local else _hosted_parts(state, questions)
    )

    try:
        resp = await _post_once(url, headers, body, timeout, deadline, _transport)
    except httpx.ConnectError as e:
        if local and plugin_config.jev_api_key:
            # 本地后端没启动：回落 OpenRouter，只回落这一次（连不上说明网络问题）
            logger.warning("本地 Jev 后端连不上（%.120s），回落 OpenRouter", e)
            url, headers, body = _hosted_parts(state, questions)
            local = False
            try:
                resp = await _post_once(url, headers, body, timeout, deadline, _transport)
            except httpx.ConnectError as e2:
                raise RuntimeError("连不上 Jev 接口，检查网络") from e2
        elif local:
            raise RuntimeError(
                "连不上本地 Jev 后端（jev_local_url），确认本地 laya 模型的 "
                "推理服务已经启动，或填 jev_api_key 走 OpenRouter"
            ) from e
        else:
            raise RuntimeError("连不上 Jev 接口，检查网络") from e

    if not local:
        if resp.status_code == 401:
            raise RuntimeError("Jev 认证失败：检查 jev_api_key")
        if resp.status_code == 422:
            raise RuntimeError(f"Jev 请求不合法：{resp.text[:120]}")
        if resp.status_code in RETRY_STATUS:
            raise RuntimeError("Jev 限流/过载，稍后再试")
    if resp.status_code >= 400:
        # 本地后端的错误都在 {"error": "..."} 里，比裸状态码可读
        try:
            detail = (resp.json() or {}).get("error")
        except Exception:
            detail = None
        if detail:
            raise RuntimeError(f"Jev 后端错误：{str(detail)[:120]}")
    resp.raise_for_status()
    return resp.json()


async def _post_with_retries(
    url: str,
    body: dict,
    headers: dict,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    resp: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for attempt in range(3):
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code in RETRY_STATUS and attempt < 2:
                delay = 2**attempt
                logger.warning("Jev %d，%.0f 秒后重试", resp.status_code, delay)
                await _sleep(delay)
                continue
            break
    assert resp is not None
    return resp
