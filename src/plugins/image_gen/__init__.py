"""图像生成插件：/image 描述（/生图 同义）——调 OpenAI images/generations 兼容接口生图。

- 未配置 image_gen_api_key / image_gen_model 时提示怎么配，不报错刷屏
- 并发生图用锁排队：多数图像服务（本地网关/厂商并发限流）不接受同一 key
  同时出图，串行后每个请求依然各自生成、各自发送，先到先画
- 图片是服务生成的二进制，回群里不经过文本净化（不是文字内容）；
  提示词只发给操作者自己配置的图像服务
- 按群限流（image_gen_rate_per_min）+ 群级开关（/bot off 生图）
"""

import asyncio
import logging
import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg

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


@draw_cmd.handle()
async def handle_draw(event: MessageEvent, args: Message = CommandArg()):
    prompt = str(args).strip()[:MAX_PROMPT]
    if not prompt:
        await draw_cmd.finish(USAGE)
    if not image_gen.image_gen_ready():
        await draw_cmd.finish("画图服务还没配置好，生不了哦")
    if not _limiter.check(
        event.group_id if isinstance(event, GroupMessageEvent) else f"u:{event.user_id}"
    ):
        await draw_cmd.finish("生图太频繁啦，一分钟后再试")

    queued = "（前面还有一张在生成，会自动排队）" if _gen_lock.locked() else ""
    await send_notice(draw_cmd, event, f"🎨 正在生图，稍等一会儿…{queued}")
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
        await draw_cmd.finish(
            f"生图超时了（超过 {int(elapsed)} 秒），服务可能比较慢，稍后再试一次"
        )
    except Exception:
        logger.exception("图像生成失败，提示词：%s", prompt[:80])
        await draw_cmd.finish("生图出错了，稍后再试试换个描述")

    await draw_cmd.send(MessageSegment.reply(event.message_id) + MessageSegment.image(data))
    await draw_cmd.finish()
