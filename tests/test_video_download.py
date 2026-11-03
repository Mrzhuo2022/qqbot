"""B站视频下载的文件解析测试（不联网，只测落盘文件判定逻辑）。"""

import asyncio
from typing import ClassVar

import pytest

from src.plugins.video_parser import (
    _aid_to_bvid,
    _resolve_bvid,
    _resolve_video_file,
    bili_url_from_json_card,
    bili_url_from_text,
)


def test_resolve_returns_existing_file(tmp_path):
    f = tmp_path / "BV123.mp4"
    f.write_bytes(b"x" * 100)
    assert _resolve_video_file({"filepath": str(f)}, max_mb=10) == f


def test_resolve_rejects_missing_file():
    with pytest.raises(ValueError, match=r"超过|登录"):
        _resolve_video_file({"filepath": "Z:/no/such/file.mp4"}, max_mb=10)
    with pytest.raises(ValueError):
        _resolve_video_file({}, max_mb=10)


def test_resolve_rejects_oversized_and_cleans(tmp_path):
    f = tmp_path / "big.mp4"
    f.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="上限"):
        _resolve_video_file({"filepath": str(f)}, max_mb=1)
    assert not f.exists()


def test_resolve_falls_back_to_filename_key(tmp_path):
    f = tmp_path / "BV456.mp4"
    f.write_bytes(b"x" * 50)
    assert _resolve_video_file({"_filename": str(f)}, max_mb=10) == f


def test_bili_url_from_text_bare_bv():
    assert bili_url_from_text("BV1U8tz6AE1o") == "https://www.bilibili.com/video/BV1U8tz6AE1o"
    assert (
        bili_url_from_text("看这个 BV1U8tz6AE1o 哈哈")
        == "https://www.bilibili.com/video/BV1U8tz6AE1o"
    )


def test_bili_url_from_text_prefers_url():
    assert (
        bili_url_from_text("https://www.bilibili.com/video/BV1U8tz6AE1o/")
        == "https://www.bilibili.com/video/BV1U8tz6AE1o/"
    )


def test_bili_url_from_text_ignores_non_bili():
    assert bili_url_from_text("https://www.google.com/BV1234567890") is None
    assert bili_url_from_text("随便聊聊") is None


def test_bili_url_from_json_card():
    class FakeSeg:
        data: ClassVar = {
            "data": (
                '{"meta":{"news":{"title":"某视频",'
                '"jump_url":"https://b23.tv/abc123"}}}'
            )
        }

    assert bili_url_from_json_card(FakeSeg()) == "https://b23.tv/abc123"


def test_bili_url_from_json_card_invalid():
    class FakeSeg:
        data: ClassVar = {"data": "不是json"}

    assert bili_url_from_json_card(FakeSeg()) is None
    assert bili_url_from_json_card(type("S", (), {"data": {}})()) is None


def _seg(raw: str):
    return type("S", (), {"data": {"data": raw}})()


def test_bili_url_from_miniapp_card_schemeless():
    # QQ 小程序卡：jump_url 常无协议头（"b23.tv/xxx"），旧逻辑要求 https:// 开头所以漏掉
    raw = (
        '{"app":"com.tencent.miniapp_01","desc":"张顺飞第一次表白成功！"'
        ',"meta":{"miniapp":{"title":"张顺飞…","jump_url":"b23.tv/1Vp8xDW?share_source=qq"}}}'
    )
    assert bili_url_from_json_card(_seg(raw)) == "https://b23.tv/1Vp8xDW"


def test_bili_url_from_miniapp_card_escaped_schemeless():
    # 同上，且 JSON 里斜杠带 \/ 转义（NapCat 原始报文常见）
    raw = '{"meta":{"miniapp":{"jump_url":"b23.tv\\/1Vp8xDW"}}}'
    assert bili_url_from_json_card(_seg(raw)) == "https://b23.tv/1Vp8xDW"


def test_bili_url_from_card_bare_bv():
    raw = '{"meta":{"detail_1":{"desc":"BV1xx411c7mD 好活"}}}'
    assert bili_url_from_json_card(_seg(raw)) == "https://www.bilibili.com/video/BV1xx411c7mD"


def test_bili_url_from_card_av():
    raw = '{"app":"com.tencent.miniapp_01","desc":"av170001 的视频"}'
    assert bili_url_from_json_card(_seg(raw)) == "https://www.bilibili.com/video/av170001"


def test_bili_url_from_card_rejects_lookalike():
    # notb23.tv / 词中 av 不算 B 站目标
    raw = '{"meta":{"miniapp":{"jump_url":"notb23.tv/abc123","desc":"nav123"}}}'
    assert bili_url_from_json_card(_seg(raw)) is None


def test_resolve_bvid_direct_bv():
    assert (
        asyncio.run(_resolve_bvid("https://www.bilibili.com/video/BV1AbCdEfGhI?p=3"))
        == "BV1AbCdEfGhI"
    )


def test_resolve_bvid_av_converts(monkeypatch):
    async def fake_aid(aid):
        assert aid == "170001"
        return "BV1AbCdEfGhI"

    monkeypatch.setattr("src.plugins.video_parser._aid_to_bvid", fake_aid)
    assert (
        asyncio.run(_resolve_bvid("https://www.bilibili.com/video/av170001"))
        == "BV1AbCdEfGhI"
    )


def test_aid_to_bvid_calls_view_api(monkeypatch):
    from src.plugins import video_parser as vp

    class FakeResp:
        def json(self):
            return {"code": 0, "data": {"bvid": "BV1AbCdEfGhI"}}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            assert url.endswith("/x/web-interface/view")
            assert params == {"aid": "170001"}
            return FakeResp()

    monkeypatch.setattr(vp.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(_aid_to_bvid("170001")) == "BV1AbCdEfGhI"


def test_cleanup_removes_expired_by_days(tmp_path, monkeypatch):
    import os
    import time as time_mod

    from src.plugins.video_parser import _cleanup_cache
    from src.shared.config import plugin_config

    monkeypatch.setattr(plugin_config, "bili_video_keep_days", 7)
    monkeypatch.setattr(plugin_config, "bili_video_cache_mb", 0)

    old = tmp_path / "BVold.mp4"
    old.write_bytes(b"x" * 100)
    fresh = tmp_path / "BVnew.mp4"
    fresh.write_bytes(b"x" * 100)
    past = time_mod.time() - 8 * 86400
    os.utime(old, (past, past))

    assert _cleanup_cache(tmp_path) == 1
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_caps_total_size_deleting_oldest(tmp_path, monkeypatch):
    import os
    import time as time_mod

    from src.plugins.video_parser import _cleanup_cache
    from src.shared.config import plugin_config

    monkeypatch.setattr(plugin_config, "bili_video_keep_days", 0)
    monkeypatch.setattr(plugin_config, "bili_video_cache_mb", 1)

    files = []
    for i, name in enumerate(["BVa", "BVb", "BVc"]):
        p = tmp_path / f"{name}.mp4"
        p.write_bytes(b"x" * 600 * 1024)  # 各 600KB，共 1.8MB > 1MB
        past = time_mod.time() - (3 - i) * 3600
        os.utime(p, (past, past))
        files.append(p)

    assert _cleanup_cache(tmp_path) == 2  # 删到 1MB 以下
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()


def test_cleanup_noop_when_under_limits(tmp_path, monkeypatch):
    from src.plugins.video_parser import _cleanup_cache
    from src.shared.config import plugin_config

    monkeypatch.setattr(plugin_config, "bili_video_keep_days", 7)
    monkeypatch.setattr(plugin_config, "bili_video_cache_mb", 500)
    p = tmp_path / "BVx.mp4"
    p.write_bytes(b"x" * 100)

    assert _cleanup_cache(tmp_path) == 0
    assert p.exists()


def test_compose_reply_only_title_and_summary():
    from src.plugins.video_parser import _compose_reply

    reply = _compose_reply("测试视频", "标题：测试视频\n简介：很长很长", "AI的分析", "", True)
    assert reply == "🎬 测试视频\n—————————\nAI的分析"
    assert "简介：" not in reply
    assert "很长很长" not in reply


def test_compose_reply_fallback_without_llm():
    from src.plugins.video_parser import _compose_reply

    reply = _compose_reply("测试视频", "标题：测试视频\n简介：desc", "", "", False)
    assert "🎬 视频解析" in reply
    assert "标题：测试视频" in reply
    assert "AI 总结还没配置" in reply


def test_compose_reply_llm_failure_falls_back():
    from src.plugins.video_parser import _compose_reply

    reply = _compose_reply("测试视频", "标题：测试视频\n简介：desc", "", "", True)
    assert "🎬 视频解析" in reply
    assert "未配置" not in reply
