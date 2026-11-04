"""图像生成插件：/image 描述（/生图 同义）——调 OpenAI images/generations 兼容接口生图。

- 自然语言也能触发：「画一张戴墨镜的橘猫」「帮我画个夕阳」（parse_nl_prompt，
  问句不触发；未配置服务时保持沉默不接话）；受同一群级开关/限流管
- 未配置 image_gen_api_key / image_gen_model 时提示怎么配，不报错刷屏
- 并发生图用锁排队：多数图像服务（本地网关/厂商并发限流）不接受同一 key
  同时出图，串行后每个请求依然各自生成、各自发送，先到先画
- 图片是服务生成的二进制，回群里不经过文本净化（不是文字内容）；
  提示词只发给操作者自己配置的图像服务
- 按群限流（image_gen_rate_per_min）+ 群级开关（/bot off 生图）
"""

import asyncio
import logging
import re
import time

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule

from src.shared import image_gen
from src.shared.config import plugin_config
from src.shared.rate_limit import RateLimiter
from src.shared.utils import group_plugin_allowed, send_notice

logger = logging.getLogger("image_gen_plugin")

MAX_PROMPT = 200
USAGE = "用法：/image 描述（/生图 同义），如 /image 一只戴墨镜的橘猫在打鼓"

_limiter = RateLimiter(plugin_config.image_gen_rate_per_min, 60)
_gen_lock = asyncio.Lock()

# 优先级 4：抢在 chat(5) 之前，避免 "/image" 被当成聊天内容
draw_cmd = on_command(
    "image", aliases={"生图"}, rule=group_plugin_allowed("image_gen"), priority=4, block=True
)


# ── 自然语言触发：「画一张X / 画个X / 帮我画一张X」─────────────

# 必须带量词：防止「画画好难」「画得不错」这类闲聊被当成生图请求
_NL_DRAW_RE = re.compile(
    r"^(?:帮我|给我|麻烦|帮忙)?"
    r"画(?:一张|一幅|一个|个|张|只|幅|副|位|朵|辆|栋|场|尊)"
    r"(?P<prompt>.+)$"
)


def parse_nl_prompt(text: str) -> str | None:
    """自然语言生图解析：命中返回提示词，否则 None（纯函数，供单测）。

    只认祈使句式；问句（含 ？ 或以疑问词收尾）和命令（/ 开头）不触发。
    """
    text = text.strip()
    if not text or text.startswith("/"):
        return None
    question_tail = (
        "吗", "么", "呢", "要多久", "多久", "怎么样", "怎样", "如何",
        "什么", "啥", "行不行", "好不好", "可不可以", "能不能",
    )
    if "？" in text or text.endswith(question_tail):
        return None
    m = _NL_DRAW_RE.match(text)
    if not m:
        return None
    prompt = m.group("prompt").strip().rstrip("吧。！!～~，,、：:的")
    return prompt[:MAX_PROMPT] if prompt else None


def _nl_draw_ready(event: MessageEvent) -> bool:
    """内容规则：服务已配置且消息是祈使式生图请求；未配置时保持沉默不接话。"""
    return image_gen.image_gen_ready() and parse_nl_prompt(event.get_plaintext()) is not None


# 同为优先级 4：命令 matcher 先注册，"/生图 ..." 会被命令先吃掉不会重复触发
nl_draw = on_message(
    rule=Rule(group_plugin_allowed("image_gen"), _nl_draw_ready), priority=4, block=True
)


@nl_draw.handle()
async def handle_nl_draw(event: MessageEvent):
    prompt = parse_nl_prompt(event.get_plaintext())
    if prompt is not None:
        await _do_draw(nl_draw, event, prompt)
    await nl_draw.finish()


async def _do_draw(matcher, event: MessageEvent, prompt: str) -> None:
    """生成并发图：命令与自然语言触发共用（限流/进度提示/错误文案一致）。"""
    if not _limiter.check(
        event.group_id if isinstance(event, GroupMessageEvent) else f"u:{event.user_id}"
    ):
        await matcher.finish("生图太频繁啦，一分钟后再试")

    queued = "（前面还有一张在生成，会自动排队）" if _gen_lock.locked() else ""
    await send_notice(matcher, event, f"🎨 正在生图，稍等一会儿…{queued}")
    start = time.monotonic()
    try:
        async with _gen_lock:
            start = time.monotonic()  # 重置为纯生成起点，排队等待不计入超时
            data = await image_gen.generate(prompt)
    # asyncio.TimeoutError 在 3.10 上与内置 TimeoutError 不是同一个类，都要列出
    except (asyncio.TimeoutError, TimeoutError):
        elapsed = time.monotonic() - start
        logger.warning(
            "图像生成超时（%.0f 秒），提示词：%s（服务慢就把 image_gen_timeout 调大）",
            elapsed, prompt[:80],
        )
        await matcher.finish(
            f"生图超时了（超过 {int(elapsed)} 秒），服务可能比较慢，稍后再试一次"
        )
    except Exception:
        logger.exception("图像生成失败，提示词：%s", prompt[:80])
        await matcher.finish("生图出错了，稍后再试试换个描述")

    await matcher.send(MessageSegment.reply(event.message_id) + MessageSegment.image(data))
    await matcher.finish()


@draw_cmd.handle()
async def handle_draw(event: MessageEvent, args: Message = CommandArg()):
    prompt = str(args).strip()[:MAX_PROMPT]
    if not prompt:
        await draw_cmd.finish(USAGE)
    if not image_gen.image_gen_ready():
        await draw_cmd.finish("画图服务还没配置好，生不了哦")
    await _do_draw(draw_cmd, event, prompt)
