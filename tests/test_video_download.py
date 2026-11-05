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


def test_bili_url_from_text_bare_av():
    assert (
        bili_url_from_text("快看av170001")
        == "https://www.bilibili.com/video/av170001"
    )
    # 前面贴着字母数字的不算（"nav170001"）
    assert bili_url_from_text("nav170001") is None


def test_bili_url_from_text_bare_b23():
    r"""无协议头短链：中文紧贴着写也要能认（\w 含 CJK，lookbehind 用 ASCII 类）。"""
    assert (
        bili_url_from_text("看这个b23.tv/WA4mdJJ 哈哈") == "https://b23.tv/WA4mdJJ"
    )
    # 前面贴着字母/点的不算（"notb23.tv"、"x.b23.tv"）
    assert bili_url_from_text("notb23.tv/abc") is None


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


# ── 短链 → BV 号解析（b23.tv 跳转）─────────────────────────


def test_resolve_bvid_direct_bv_no_fetch(monkeypatch):
    """URL 里已带 BV 号：直接返回，不发任何请求。"""
    import asyncio

    from src.plugins import video_parser as vp

    def _no_fetch(*a, **kw):
        raise AssertionError("不应发起网络请求")

    monkeypatch.setattr(vp, "safe_fetch", _no_fetch)
    # 短链无 BV/av 字样 → 走 fetch 路径（被上面的桩拦下）→ 返回 None
    assert asyncio.run(vp._resolve_bvid("https://b23.tv/x")) is None
    got = asyncio.run(vp._resolve_bvid("https://www.bilibili.com/video/BV1GJ411x7h7/?p=1"))
    assert got == "BV1GJ411x7h7"


def test_resolve_bvid_short_link_skips_body(monkeypatch):
    """短链跟随只要最终 URL：read_body=False，正文不读不解码（免疫 CDN 压缩编码问题）。"""
    import asyncio

    from src.plugins import video_parser as vp

    captured: dict = {}

    class _Resp:
        url = "https://www.bilibili.com/video/BV1UFnn6xE7A/?buvid=xx&p=1"

    async def fake_safe_fetch(url, **kw):
        captured["read_body"] = kw.get("read_body")
        return _Resp()

    monkeypatch.setattr(vp, "safe_fetch", fake_safe_fetch)
    got = asyncio.run(vp._resolve_bvid("https://b23.tv/DVRAWwT?share_source=qq"))
    assert got == "BV1UFnn6xE7A"
    assert captured["read_body"] is False


def test_resolve_bvid_short_link_av_target(monkeypatch):
    """短链跳到 av 形态目标页：av 号兜底换 BV，而不是直接放弃。"""
    import asyncio

    from src.plugins import video_parser as vp

    class _Resp:
        url = "https://www.bilibili.com/video/av170001/?p=1"

    async def fake_safe_fetch(url, **kw):
        return _Resp()

    async def fake_aid_to_bvid(aid):
        assert aid == "170001"
        return "BV17x411w7KC"

    monkeypatch.setattr(vp, "safe_fetch", fake_safe_fetch)
    monkeypatch.setattr(vp, "_aid_to_bvid", fake_aid_to_bvid)
    got = asyncio.run(vp._resolve_bvid("https://b23.tv/abc123"))
    assert got == "BV17x411w7KC"


# ── 动态链接解析（b23 跳到 /opus/<id>）────────────────────


def _dyn_item(
    major_type: str,
    bvid: str | None = None,
    author: str = "UP主",
    desc: str = "",
    image_srcs: list[str] | None = None,
    orig=None,
):
    major = {"type": major_type}
    if bvid:
        major["archive"] = {"bvid": bvid}
    if image_srcs:
        major["draw"] = {"items": [{"src": s} for s in image_srcs]}
    dyn = {"major": major}
    if desc:
        dyn["desc"] = {"text": desc}
    item = {"modules": {"module_author": {"name": author}, "module_dynamic": dyn}}
    if orig is not None:
        item["orig_item"] = orig
    return item


def test_parse_dynamic_item_video():
    from src.plugins.video_parser import _parse_dynamic_item

    bvid, content, label = _parse_dynamic_item(_dyn_item("MAJOR_TYPE_ARCHIVE", "BV1yZ4u6BEDS"))
    assert bvid == "BV1yZ4u6BEDS"
    assert content is None
    assert label == "UP主的视频动态"


def test_parse_dynamic_item_draw():
    """纯图动态（无文字）：内容里只有图片，文字为空串。"""
    from src.plugins.video_parser import _parse_dynamic_item

    bvid, content, label = _parse_dynamic_item(
        _dyn_item(
            "MAJOR_TYPE_DRAW",
            author="织部Kaka",
            image_srcs=["https://i0.hdslb.com/bfs/a.jpg"],
        )
    )
    assert bvid is None
    assert label == "织部Kaka的图文动态"
    assert content["author"] == "织部Kaka"
    assert content["text"] == ""
    assert content["images"] == ["https://i0.hdslb.com/bfs/a.jpg"]


def test_parse_dynamic_item_text_with_opus():
    """新版图文动态：文字在 opus.title/summary 里，desc 反而为空。"""
    from src.plugins.video_parser import _parse_dynamic_item

    item = _dyn_item("MAJOR_TYPE_DRAW")
    item["modules"]["module_dynamic"]["major"]["opus"] = {
        "title": "标题",
        "summary": {"text": "正文内容"},
    }
    bvid, content, _label = _parse_dynamic_item(item)
    assert bvid is None
    assert content["text"] == "标题\n正文内容"


def test_parse_dynamic_item_forward_content():
    """转发图文：转发者的评论 + 原动态的文字图片都收集。"""
    from src.plugins.video_parser import _parse_dynamic_item

    fwd = _dyn_item(
        "MAJOR_TYPE_FORWARD",
        desc="转发理由",
        orig=_dyn_item(
            "MAJOR_TYPE_DRAW",
            desc="原文内容",
            image_srcs=["https://i0.hdslb.com/bfs/x.jpg", "https://i0.hdslb.com/bfs/y.jpg"],
        ),
    )
    bvid, content, label = _parse_dynamic_item(fwd)
    assert bvid is None
    assert label == "UP主的转发动态"
    assert content["text"] == "转发理由\n原文内容"
    assert len(content["images"]) == 2


def test_parse_dynamic_item_images_domain_filter():
    """非 B 站 CDN 的图片 URL 一律丢弃（防 SSRF 姿态，URL 由协议端直接抓）。"""
    from src.plugins.video_parser import _parse_dynamic_item

    _bvid, content, _label = _parse_dynamic_item(
        _dyn_item(
            "MAJOR_TYPE_DRAW",
            desc="有文字所以 content 不为空",
            image_srcs=[
                "https://i0.hdslb.com/bfs/ok.jpg",
                "http://evil.example.com/a.jpg",
                "https://bilibili.com.evil.example.com/b.jpg",
            ],
        )
    )
    assert content["images"] == ["https://i0.hdslb.com/bfs/ok.jpg"]


def test_parse_dynamic_item_forward_video():
    """转发动态：视频在被转发的原动态里，也要能取到。"""
    from src.plugins.video_parser import _parse_dynamic_item

    fwd = _dyn_item(
        "MAJOR_TYPE_FORWARD", orig=_dyn_item("MAJOR_TYPE_ARCHIVE", "BV1GJ411x7h7")
    )
    bvid, content, label = _parse_dynamic_item(fwd)
    assert bvid == "BV1GJ411x7h7"
    assert content is None
    assert label == "UP主的转发视频动态"


def test_parse_dynamic_item_live_no_content():
    """直播/专栏这类没有可转发文字图片的：content 为 None，走类型提示。"""
    from src.plugins.video_parser import _parse_dynamic_item

    bvid, content, label = _parse_dynamic_item(_dyn_item("MAJOR_TYPE_LIVE_RCMD"))
    assert bvid is None
    assert content is None
    assert label == "UP主的直播动态"


def test_parse_dynamic_item_sanitize_author():
    """作者名是不可信输入：CQ 码被洗掉、换行折叠、超长截断。"""
    from src.plugins.video_parser import _parse_dynamic_item

    bvid, _content, label = _parse_dynamic_item(
        _dyn_item("MAJOR_TYPE_DRAW", author="[CQ:at,qq=all]\n假装管理员", desc="x")
    )
    assert bvid is None
    assert label == "假装管理员的图文动态"


def test_resolve_bvid_dynamic_with_video(monkeypatch):
    """短链跳到动态页且动态带视频：取出 BV，后续走正常视频流程。"""
    import asyncio

    from src.plugins import video_parser as vp

    class _Resp:
        url = "https://www.bilibili.com/opus/1267183362902065160?share_from=dynamic"

    async def fake_safe_fetch(url, **kw):
        return _Resp()

    async def fake_lookup(dyn_id):
        assert dyn_id == "1267183362902065160"
        return "BV1yZ4u6BEDS", None, "UP主的视频动态"

    monkeypatch.setattr(vp, "safe_fetch", fake_safe_fetch)
    monkeypatch.setattr(vp, "_dynamic_lookup", fake_lookup)
    got = asyncio.run(vp._resolve_bvid("https://b23.tv/WA4mdJJ?share_source=qq"))
    assert got == "BV1yZ4u6BEDS"


def test_resolve_bvid_dynamic_draw_raises(monkeypatch):
    """短链跳到纯图文动态：抛 NotAVideoLink 且带出可转发内容。"""
    import asyncio

    import pytest

    from src.plugins import video_parser as vp
    from src.plugins.video_parser import NotAVideoLink

    class _Resp:
        url = "https://www.bilibili.com/opus/1267183362902065160"

    async def fake_safe_fetch(url, **kw):
        return _Resp()

    async def fake_lookup(dyn_id):
        return None, {
            "author": "织部Kaka",
            "label": "织部Kaka的图文动态",
            "text": "",
            "images": ["https://i0.hdslb.com/bfs/a.jpg"],
        }, "织部Kaka的图文动态"

    monkeypatch.setattr(vp, "safe_fetch", fake_safe_fetch)
    monkeypatch.setattr(vp, "_dynamic_lookup", fake_lookup)
    with pytest.raises(NotAVideoLink, match="织部Kaka的图文动态，没有视频") as ei:
        asyncio.run(vp._resolve_bvid("https://b23.tv/WA4mdJJ"))
    assert ei.value.content["images"] == ["https://i0.hdslb.com/bfs/a.jpg"]


def test_resolve_bvid_dynamic_lookup_fail(monkeypatch):
    """动态接口失败（风控/删除）：静默回 None，走通用失败提示，不冒充「不是视频」。"""
    import asyncio

    from src.plugins import video_parser as vp

    class _Resp:
        url = "https://www.bilibili.com/opus/1267183362902065160"

    async def fake_safe_fetch(url, **kw):
        return _Resp()

    async def fake_lookup(dyn_id):
        return None

    monkeypatch.setattr(vp, "safe_fetch", fake_safe_fetch)
    monkeypatch.setattr(vp, "_dynamic_lookup", fake_lookup)
    assert asyncio.run(vp._resolve_bvid("https://b23.tv/WA4mdJJ")) is None


def test_resolve_bvid_t_bilibili_direct(monkeypatch):
    """t.bilibili.com 直链（实测 200 不跳转）：不走 safe_fetch，直接查动态接口。"""
    import asyncio

    from src.plugins import video_parser as vp

    def _no_fetch(*a, **kw):
        raise AssertionError("t.bilibili 直链不应发起跳转请求")

    async def fake_dynamic_bvid(dyn_id):
        assert dyn_id == "1267183362902065160"
        return "BV1yZ4u6BEDS"

    monkeypatch.setattr(vp, "safe_fetch", _no_fetch)
    monkeypatch.setattr(vp, "_dynamic_bvid", fake_dynamic_bvid)
    got = asyncio.run(vp._resolve_bvid("https://t.bilibili.com/1267183362902065160"))
    assert got == "BV1yZ4u6BEDS"


def test_resolve_bvid_opus_direct_no_fetch(monkeypatch):
    """opus 直链同样不用走跳转请求。"""
    import asyncio

    from src.plugins import video_parser as vp

    def _no_fetch(*a, **kw):
        raise AssertionError("opus 直链不应发起跳转请求")

    async def fake_dynamic_bvid(dyn_id):
        return None

    monkeypatch.setattr(vp, "safe_fetch", _no_fetch)
    monkeypatch.setattr(vp, "_dynamic_bvid", fake_dynamic_bvid)
    assert (
        asyncio.run(vp._resolve_bvid("https://www.bilibili.com/opus/1267183362902065160"))
        is None
    )


def test_resolve_bvid_non_video_labels(monkeypatch):
    """直播/番剧/专栏跳转后：说明类型，不带可转发内容。"""
    import asyncio

    import pytest

    from src.plugins import video_parser as vp
    from src.plugins.video_parser import NotAVideoLink

    cases = [
        ("https://live.bilibili.com/6?share_source=copy_link", "直播间链接"),
        ("https://www.bilibili.com/bangumi/play/ep477297", "番剧/影视链接"),
        ("https://www.bilibili.com/read/cv25000000/", "专栏文章链接"),
    ]

    class _Resp:
        def __init__(self, url):
            self.url = url

    def _make_fetch(final_url):
        async def fake_safe_fetch(u, **kw):
            return _Resp(final_url)

        return fake_safe_fetch

    for final_url, label in cases:
        monkeypatch.setattr(vp, "safe_fetch", _make_fetch(final_url))
        with pytest.raises(NotAVideoLink, match=label) as ei:
            asyncio.run(vp._resolve_bvid("https://b23.tv/abc123"))
        assert ei.value.content is None
