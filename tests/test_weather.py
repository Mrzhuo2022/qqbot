"""天气插件测试：WMO 映射、风向、卡片格式化、地名解析与预报请求。"""

import asyncio

from src.plugins import weather as wx


def test_wmo_text():
    assert wx.wmo_text(0) == "晴"
    assert wx.wmo_text(61) == "小雨"
    assert wx.wmo_text(95) == "雷暴"
    assert wx.wmo_text(1234) == "未知"
    assert wx.wmo_text(None) == "未知"
    assert wx.wmo_text("61") == "小雨"


def test_wind_text_direction_and_speed():
    assert wx.wind_text(0, 10) == "北风 10 km/h"
    assert wx.wind_text(225, 12.4) == "西南风 12 km/h"
    assert wx.wind_text(350, 5) == "北风 5 km/h"
    assert wx.wind_text(None, 5) == ""


GEO = {"name": "北京", "admin1": "北京市", "latitude": 39.9, "longitude": 116.4}
DATA = {
    "current": {
        "temperature_2m": 18.3, "apparent_temperature": 16.7,
        "relative_humidity_2m": 62, "weather_code": 2,
        "wind_speed_10m": 12.4, "wind_direction_10m": 225,
    },
    "daily": {
        "weather_code": [2, 61],
        "temperature_2m_max": [22.4, 20.1],
        "temperature_2m_min": [11.8, 13.9],
    },
}


def test_format_card():
    card = wx.format_card(GEO, DATA)
    assert "北京·北京市" in card
    assert "现在：18° 局部多云（体感 17°）" in card
    assert "湿度 62%" in card
    assert "西南风 12 km/h" in card
    assert "今天：局部多云 12~22°" in card
    assert "明天：小雨 14~20°" in card


def test_format_card_survives_missing_fields():
    # 接口字段缺失时只出地名行，不抛异常
    card = wx.format_card({"name": "X"}, {})
    assert "X" in card


# ── 请求链路（假 client）─────────────────────────────────

class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, geocode, forecast):
        self.calls = []
        self._geocode = geocode
        self._forecast = forecast

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResp(self._geocode if "geocoding" in url else self._forecast)


def test_fetch_weather_requests():
    client = FakeClient({"results": [GEO]}, DATA)
    result = asyncio.run(wx.fetch_weather(client, "北京"))
    geo, _ = result
    assert geo["name"] == "北京"
    fc_url, fc_params = client.calls[1]
    assert "forecast" in fc_url
    assert fc_params["latitude"] == 39.9 and fc_params["longitude"] == 116.4
    assert fc_params["forecast_days"] == 2


def test_fetch_weather_unknown_city():
    client = FakeClient({"results": []}, DATA)
    assert asyncio.run(wx.fetch_weather(client, "不存在的城市")) is None
