"""天气插件：/weather 城市（/天气 同义）查当前天气 + 今明两天预报。

数据来自 Open-Meteo（免费、无需 API key）：先地名检索拿经纬度，
再查实时 + 逐日预报。不走 LLM；同城市 10 分钟缓存；城市名只进
query 参数（httpx 负责编码），不拼 URL，无 SSRF 面。
"""

import logging

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from src.shared.cache import TTLCache
from src.shared.config import plugin_config
from src.shared.rate_limit import RateLimiter
from src.shared.utils import UA_HEADERS, clip, group_plugin_allowed, send_reply

logger = logging.getLogger("weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

MAX_CITY_LEN = 30
_cache = TTLCache(maxsize=64, ttl=600)
_limiter = RateLimiter(6, 60)

# WMO weather code → 中文
WMO_TEXT = {
    0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    56: "冻毛毛雨", 57: "浓冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

# 风向角度 → 十六方位里的八个
WIND_DIRS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


def wmo_text(code) -> str:
    try:
        return WMO_TEXT.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


def wind_text(deg, speed) -> str:
    """「西南风 12 km/h」；缺数据返回空串。"""
    try:
        idx = int((int(deg) + 22.5) // 45) % 8
        return f"{WIND_DIRS[idx]}风 {int(speed)} km/h"
    except (TypeError, ValueError):
        return ""


async def _geocode(client: httpx.AsyncClient, city: str) -> dict | None:
    resp = await client.get(
        GEOCODE_URL,
        params={"name": city, "count": 1, "language": "zh", "format": "json"},
    )
    results = resp.json().get("results") or []
    return results[0] if results else None


async def fetch_weather(client: httpx.AsyncClient, city: str) -> tuple[dict, dict] | None:
    """地名 → (地理信息, 预报数据)；查不到城市或接口失败返回 None。"""
    geo = await _geocode(client, city)
    if not geo:
        return None
    resp = await client.get(
        FORECAST_URL,
        params={
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "current": ",".join((
                "temperature_2m", "apparent_temperature",
                "relative_humidity_2m", "weather_code",
                "wind_speed_10m", "wind_direction_10m",
            )),
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "forecast_days": 2,
            "timezone": "auto",
        },
    )
    return geo, resp.json()


def format_card(geo: dict, data: dict) -> str:
    """Open-Meteo 响应 → 出群卡片。"""
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    place = str(geo.get("name") or "未知地点")
    admin = str(geo.get("admin1") or "").strip()
    if admin and admin != place:
        place = f"{place}·{admin}"

    lines = [f"🌤 {place}"]
    temp = cur.get("temperature_2m")
    if temp is not None:
        feels = cur.get("apparent_temperature")
        feel_s = f"（体感 {round(feels)}°）" if feels is not None else ""
        lines.append(f"现在：{round(temp)}° {wmo_text(cur.get('weather_code'))}{feel_s}")
    humidity = cur.get("relative_humidity_2m")
    wind = wind_text(cur.get("wind_direction_10m"), cur.get("wind_speed_10m"))
    extras = []
    if humidity is not None:
        extras.append(f"湿度 {round(humidity)}%")
    if wind:
        extras.append(wind)
    if extras:
        lines.append(" · ".join(extras))

    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    for i, label in enumerate(("今天", "明天")):
        if i < len(codes) and i < len(highs) and i < len(lows):
            try:
                lines.append(
                    f"{label}：{wmo_text(codes[i])} {round(lows[i])}~{round(highs[i])}°"
                )
            except (TypeError, ValueError):
                continue
    return clip("\n".join(lines), plugin_config.reply_max_chars)


# 优先级 4：抢在 chat(5) 之前，避免 "/weather" 被当成聊天内容
weather_cmd = on_command(
    "weather", aliases={"天气"}, rule=group_plugin_allowed("weather"), priority=4, block=True
)


@weather_cmd.handle()
async def handle_weather(event: MessageEvent, args: Message = CommandArg()):
    city = str(args).strip()[:MAX_CITY_LEN]
    if not city:
        await weather_cmd.finish("用法：/weather 城市，如 /weather 北京（/天气 同义）")
    gid = getattr(event, "group_id", None) or f"u:{event.user_id}"
    if not _limiter.check(gid):
        await weather_cmd.finish("查得太频繁啦，一分钟后再试")

    cached = _cache.get(city)
    if cached is not None:
        await send_reply(weather_cmd, event, cached, plugin_config.reply_chunk_chars, quote=False)
        await weather_cmd.finish()

    try:
        async with httpx.AsyncClient(timeout=10, headers=UA_HEADERS) as client:
            result = await fetch_weather(client, city)
    except Exception as e:
        logger.warning("天气查询失败 %s: %s", city, e)
        await weather_cmd.finish("天气服务暂时连不上，稍后再试")
    if result is None:
        await weather_cmd.finish(f"没找到「{city}」这个地方，换个写法试试（支持国内城市和拼音）")

    card = format_card(*result)
    _cache.set(city, card)
    await send_reply(weather_cmd, event, card, plugin_config.reply_chunk_chars, quote=False)
    await weather_cmd.finish()
