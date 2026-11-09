"""TTS 语音合成：通用 OpenAI audio/speech 接口（POST {base_url}/audio/speech）。

任何兼容该格式的服务都能接（云端 TTS / 本地部署的 OpenAI 格式网关等）。
语音回复失败一律返回 None 由调用方回落文字，TTS 挂了不能影响 bot 回话。
"""

import asyncio
import io
import logging
import re
import wave

from openai import AsyncOpenAI, BadRequestError

from src.shared import storage
from src.shared.config import plugin_config

logger = logging.getLogger("tts")

_client_instance: AsyncOpenAI | None = None

# 首尾静音垫片（秒）：TTS 输出首尾音节容易被 silk 转码/QQ 播放削掉，垫点冗余；
# 尾部比头部留长些（结尾被切更难忍）
PAD_HEAD_S = 0.15
PAD_TAIL_S = 0.25

# 语音里没法听的元素：链接原样念出来是乱码，CQ 码是内部协议文本。
# URL 排除 CJK/中文标点/全角符号/引号：`\S+` 会把「https://x.com/a。好的」整句吞掉
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef<>\"']+")
_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
_WS_RE = re.compile(r"\s+")


def tts_ready() -> bool:
    return bool(plugin_config.tts_model)


def _timeout() -> float:
    # 防止把 tts_timeout 配成 0/负数后请求瞬间超时
    return max(float(plugin_config.tts_timeout), 10.0)


def _client(timeout: float) -> AsyncOpenAI:
    global _client_instance
    if _client_instance is None:
        # max_retries=0：慢端点不做 SDK 级静默重试（会成倍拉长回复等待），
        # 超时快速失败，由调用方回落文字
        _client_instance = AsyncOpenAI(
            api_key=plugin_config.tts_api_key or "none",
            base_url=plugin_config.tts_api_url or None,  # 留空走 OpenAI 官方
            timeout=timeout,
            max_retries=0,
        )
    return _client_instance


def speech_text(text: str) -> str:
    """净化成适合朗读的文本：剥链接/CQ 码、压空白、按上限截断。"""
    text = _CQ_RE.sub("", _URL_RE.sub("", text))
    text = _WS_RE.sub(" ", text).strip()
    limit = max(plugin_config.tts_max_chars, 10)
    return text[:limit]


def voice_reply_on(group_id: int) -> bool:
    """该群是否开启语音回复（voice 功能的群级开关 + 服务就绪判断）。"""
    return tts_ready() and storage.group_plugin_enabled_id(group_id, "voice")


def _pad_silence(data: bytes) -> bytes:
    """给 WAV 首尾垫静音，防首尾音节被削；非标准 PCM WAV 原样返回（尽力而为）。"""
    try:
        with wave.open(io.BytesIO(data)) as r:
            params = r.getparams()
            pcm = r.readframes(r.getnframes())
    except Exception:
        return data
    # 8-bit WAV 是 unsigned，静音是 0x80；16/24/32-bit 的静音是全零
    fill = b"\x80" if params.sampwidth == 1 else b"\x00"
    unit = params.nchannels * params.sampwidth
    head = fill * (int(params.framerate * PAD_HEAD_S) * unit)
    tail = fill * (int(params.framerate * PAD_TAIL_S) * unit)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(head + pcm + tail)
    return out.getvalue()


async def synthesize(text: str) -> bytes | None:
    """文本转音频字节；失败返回 None（调用方回落文字），不抛异常。"""
    if not tts_ready():
        return None
    client = _client(_timeout())
    try:
        resp = await asyncio.wait_for(
            client.audio.speech.create(
                model=plugin_config.tts_model,
                input=text,
                voice=plugin_config.tts_voice,
                response_format="wav",
                speed=plugin_config.tts_speed,
            ),
            timeout=_timeout(),
        )
        return _pad_silence(resp.content)
    except BadRequestError:
        # 有些兼容网关不认 speed/response_format 这类参数；去掉重试一次，
        # 格式交给服务默认（wav 念不了就整体回落，由调用方处理）
        try:
            logger.info("TTS 接口拒绝了扩展参数，改用最小参数重试")
            resp = await asyncio.wait_for(
                client.audio.speech.create(
                    model=plugin_config.tts_model,
                    input=text,
                    voice=plugin_config.tts_voice,
                ),
                timeout=_timeout(),
            )
            return _pad_silence(resp.content)
        except Exception as e:
            logger.warning("TTS 最小参数重试仍失败: %s", e)
            return None
    except Exception as e:
        logger.warning("TTS 合成失败: %s", e)
        return None
