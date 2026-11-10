"""媒体平台解析插件：小红书 / 抖音 / 小黑盒 的分享链接。

三家都需要单独适配（各自的反爬和页面结构），统一放这里：
- 小红书：登录 cookie（xhs_cookie）抓网页版，从 __INITIAL_STATE__ 挖笔记，
  图文发文字+图；视频流分离（h264+音轨），下载后 ffmpeg 合成发送
- 抖音：登录 cookie（douyin_cookie）抓 iesdouyin 分享页，从 _ROUTER_DATA 挖；
  图集发图，视频 play 端点直下 mp4（自带音轨，无需合成）
- 小黑盒：免 cookie，SPA 页面走 api.xiaoheihe.cn 的 link/tree 接口
  （hkey 签名算法从网页版 JS 逆向，见 _heybox_hkey），文字+图直接 mirror
输出一律「内容本身」mirror（动态类内容短，不做 AI 总结）；视频照 B站视频
方式下载后作为视频消息发送。
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import shutil
import subprocess
import time as time_mod
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import FinishedException

from src.shared.cache import TTLCache
from src.shared.config import plugin_config
from src.shared.fetch import safe_fetch
from src.shared.rate_limit import RateLimiter
from src.shared.storage import group_plugin_enabled
from src.shared.utils import (
    UA_HEADERS,
    extract_first_url,
    group_allowed,
    sanitize_one_line,
    send_notice,
    send_reply,
)

logger = logging.getLogger("media_parser")

MEDIA_DOMAIN_RE = re.compile(
    r"\b(?:xhslink\.com|xiaohongshu\.com|douyin\.com|iesdouyin\.com|xiaoheihe\.cn)"
)
XHS_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{16,32})")
DY_ID_RE = re.compile(r"/(?:video|note)/(\d{15,25})")
# 小黑盒帖子：分享短码或数字 id
HEYBOX_LINK_ID_RE = re.compile(r"/(?:app/bbs/link|link)/([0-9a-f]{6,32})", re.I)
# 小红书页面状态里的字面 undefined 不是合法 JSON，先换成 null 再解析
_UNDEF_RE = re.compile(r"\bundefined\b")

VIDEO_DIR = Path("data/videos")
_DY_PLAY_API = "https://www.iesdouyin.com/aweme/v1/play/"

_limiter = RateLimiter(plugin_config.parser_rate_per_min, 60)
_dl_limiter = RateLimiter(plugin_config.bili_dl_per_min, 60)
_dl_lock = asyncio.Lock()


def _clean_cookie(raw: str) -> str:
    """Cookie 配置常见粘贴污染：首尾空白和成对引号剥掉。"""
    return (raw or "").strip().strip('"').strip("'")


@dataclass
class MediaNote:
    """解析结果缓存：命中后直接重发，不占限流额度。"""

    card: str
    images: list[str] = field(default_factory=list)
    # 视频笔记：video_url 单流直下；mix = (视频轨, 音轨) 分离流需 ffmpeg 合成
    video_url: str | None = None
    mix: tuple[str, str] | None = None
    video_tag: str = ""
    video_key: str = ""


_note_cache: "TTLCache" = TTLCache(maxsize=256, ttl=1800)


async def _has_media_url(event: MessageEvent) -> bool:
    if not group_allowed(event):
        return False
    if isinstance(event, GroupMessageEvent) and not group_plugin_enabled(event, "media_parser"):
        return False
    url = extract_first_url(event.get_plaintext())
    return bool(url and MEDIA_DOMAIN_RE.search(url))


# priority 9：抢在 link_parser(10) 之前；video_parser(4) 对抖音链接已让位
media_matcher = on_message(rule=_has_media_url, priority=9, block=False)


async def _grab(url: str, headers: dict, timeout: float = 20.0) -> str:
    """带校验抓页面正文文本（复用 safe_fetch 的 SSRF/压缩防护）。"""
    resp = await safe_fetch(url, headers=headers, timeout=timeout)
    return resp.text[:2_000_000]


async def _send_images(images: list[str]) -> None:
    """配图直接给 URL 让 QQ 客户端拉；一张失败就停，别连环刷屏。"""
    limit = plugin_config.link_parser_max_images
    for u in images[:limit]:
        try:
            await media_matcher.send(MessageSegment.image(u))
        except Exception:
            logger.warning("配图发送失败：%s", u)
            return


async def _deliver_cached(event: MessageEvent, note: MediaNote, headers: dict) -> None:
    """缓存命中重发：文字卡片 + 配图（截断）+ 视频笔记的缓存视频。"""
    await send_reply(media_matcher, event, note.card, plugin_config.reply_chunk_chars)
    await _send_images(note.images)
    if note.video_url or note.mix:
        await _deliver_video(event, note.video_tag, note.video_key, note.video_url or "", headers, note.mix)


# ── 小红书 ────────────────────────────────────────────────────────────

async def _resolve_xhs(url: str) -> str:
    """短链（xhslink.com）跟跳转拿笔记真实地址。"""
    if "xhslink.com" not in url:
        return url
    resp = await safe_fetch(url, headers=UA_HEADERS, timeout=15, read_body=False)
    return str(resp.url) if resp.url != url else url


def _parse_xhs_note(page: str, note_id: str) -> tuple[str, str, list[str], dict | None] | None:
    """__INITIAL_STATE__ → (标题, 正文, 图片, 视频信息dict)。

    网页版把笔记数据 SSR 进 noteDetailMap；纯视频笔记可能没有 imageList。
    """
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>", page, re.S)
    if not m:
        return None
    try:
        state = json.loads(_UNDEF_RE.sub("null", m.group(1)))
    except ValueError:
        logger.warning("小红书 __INITIAL_STATE__ 解析失败")
        return None
    detail_map = (state.get("note") or {}).get("noteDetailMap") or {}
    note = (detail_map.get(note_id) or {}).get("note") or {}
    if not note:
        # 请求的笔记不在首屏 map 里（罕见）：翻一遍同结构的 note 兜底
        for v in detail_map.values():
            if isinstance(v, dict) and v.get("note"):
                note = v["note"]
                break
    if not note:
        return None
    title = sanitize_one_line((note.get("title") or "").strip())
    text = (note.get("desc") or "").strip()
    images: list[str] = []
    for img in note.get("imageList") or []:
        url_default = (img or {}).get("urlDefault") or ""
        if not url_default:
            # infoList 里挑第一张可用地址
            for info in (img or {}).get("infoList") or []:
                if (info or {}).get("url"):
                    url_default = info["url"]
                    break
        if url_default.startswith("http"):
            images.append(url_default)
    return title, text, images, note.get("video") or None


def _xhs_video_streams(video: dict) -> tuple[str, str] | None:
    """分离流地址：(视频轨, 音轨)。旧字段没有则 None。"""
    stream = ((video or {}).get("media") or {}).get("stream") or {}
    v = next((s.get("masterUrl") for s in stream.get("h264") or [] if s.get("masterUrl")), None)
    a = next(
        (
            t.get("masterUrl")
            for t in (stream.get("audioTracks") or [])
            if (t or {}).get("masterUrl")
        ),
        None,
    )
    if not a:
        media = stream.get("media") or []
        a = next((m.get("masterUrl") for m in media if (m or {}).get("masterUrl")), None)
    if v and a:
        return v, a
    return None


# ── 抖音 ──────────────────────────────────────────────────────────────

async def _resolve_douyin(url: str, headers: dict) -> str:
    """v.douyin.com 短链跟跳转拿真实地址。"""
    if "v.douyin.com" not in url:
        return url
    resp = await safe_fetch(url, headers=headers, timeout=15, read_body=False)
    return str(resp.url) if resp.url != url else url


def _parse_douyin(page: str) -> tuple[str, str, list[str], str | None] | None:
    """iesdouyin 分享页 _ROUTER_DATA → (描述, 作者, 图集, 视频播放地址)。

    图文动态 play_addr 为空但 images 非空；视频时 play_addr.uri 构造播放端点。
    """
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", page, re.S)
    if not m:
        return None
    try:
        router = json.loads(m.group(1))
    except ValueError:
        logger.warning("抖音 _ROUTER_DATA 解析失败")
        return None
    item = None
    for loader in (router.get("loaderData") or {}).values():
        il = ((loader or {}).get("videoInfoRes") or {}).get("item_list") or []
        if il:
            item = il[0]
            break
    if not item:
        return None
    author = sanitize_one_line(((item.get("author") or {}).get("nickname") or "").strip())
    desc = (item.get("desc") or "").strip()
    images = [
        u
        for img in item.get("images") or []
        for u in [((img or {}).get("url_list") or [None])[0]]
        if u and u.startswith("http")
    ]
    play_uri = (((item.get("video") or {}).get("play_addr") or {}).get("uri")) or ""
    play_url = (
        f"{_DY_PLAY_API}?video_id={play_uri}&ratio=1080p&line=0" if play_uri else None
    )
    return desc, author, images, play_url


# ── 视频下载发送 ──────────────────────────────────────────────────────

def _ffmpeg() -> str | None:
    if plugin_config.ffmpeg_path:
        return plugin_config.ffmpeg_path
    return shutil.which("ffmpeg")


class VideoDownloadError(ValueError):
    """业务性下载错误（超限等），消息可直接回显群里。"""


def _download_to(url: str, dest: Path, headers: dict) -> None:
    """同步流式下载落盘（丢线程池跑），超限即断并删残件。"""
    limit = plugin_config.bili_video_max_mb * 1024 * 1024
    try:
        with httpx.Client(
            timeout=plugin_config.bili_video_timeout, headers=headers, follow_redirects=True
        ) as c:
            with c.stream("GET", url) as resp:
                resp.raise_for_status()
                written = 0
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(65536):
                        written += len(chunk)
                        if written > limit:
                            raise VideoDownloadError(
                                f"视频超过 {plugin_config.bili_video_max_mb}MB 上限"
                            )
                        f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _mix_av(video: Path, audio: Path, dest: Path) -> bool:
    """ffmpeg 合成音视频轨（小红书分离流用），失败返回 False。"""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return False
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio),
             "-c", "copy", str(dest)],
            timeout=plugin_config.bili_video_timeout,
            check=True,
        )
        return dest.exists()
    except Exception as e:
        logger.warning("ffmpeg 合成失败：%s", e)
        return False


async def _send_video_file(event: MessageEvent, path: Path) -> None:
    try:
        await media_matcher.send(MessageSegment.video(path.resolve().as_uri()))
    except Exception:
        logger.exception("视频消息发送失败")
        await send_reply(media_matcher, event, "❌ 视频下载好了但发送失败（可能超过群文件限制）", 500)


async def _deliver_video(
    event: MessageEvent, tag: str, key: str, url: str,
    headers: dict, mix: tuple[str, str] | None = None,
) -> None:
    """下载并发送视频；tag/key 用于缓存命名，mix 非空 = 分离流需 ffmpeg 合成。"""
    gid = event.group_id if isinstance(event, GroupMessageEvent) else f"u:{event.user_id}"
    if not _dl_limiter.check(gid):
        await send_reply(media_matcher, event, "视频下载太频繁啦，一分钟后再试", 500)
        return
    if _dl_lock.locked():
        await send_reply(media_matcher, event, "已经有视频在下载了，稍等它发完再试", 500)
        return
    if mix and not _ffmpeg():
        # 先检查再下载，别流量白跑一趟才发现合成不了
        await send_reply(media_matcher, event, "❌ 该视频需要 ffmpeg 合成音轨，请管理员配置 ffmpeg_path", 500)
        return
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    cached = VIDEO_DIR / f"{tag}_{key}.mp4"
    if cached.exists():
        await _send_video_file(event, cached)
        return
    limit = plugin_config.bili_video_max_mb * 1024 * 1024
    if url:
        # 下载前 HEAD 预检体积，超限提前说别白跑流量；HEAD 失败不挡下载（流式兜底）
        try:
            async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as c:
                h = await c.head(url)
            cl = h.headers.get("content-length", "")
            if cl.isdigit() and int(cl) > limit:
                await send_reply(
                    media_matcher, event,
                    f"❌ 视频有 {int(cl) / 1048576:.0f}MB，超过 {plugin_config.bili_video_max_mb}MB 上限",
                    500,
                )
                return
        except Exception:
            pass
    await send_notice(media_matcher, event, f"⬇️ 正在下载视频（≤{plugin_config.bili_video_max_mb}MB），下载好后自动发送…")
    async with _dl_lock:
        loop = asyncio.get_running_loop()
        tmp_v = VIDEO_DIR / f"_{tag}_{key}_v.tmp"
        tmp_a = VIDEO_DIR / f"_{tag}_{key}_a.tmp"
        try:
            if mix:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _download_to, mix[0], tmp_v, headers),
                    plugin_config.bili_video_timeout,
                )
                await asyncio.wait_for(
                    loop.run_in_executor(None, _download_to, mix[1], tmp_a, headers),
                    plugin_config.bili_video_timeout,
                )
                if not await loop.run_in_executor(None, _mix_av, tmp_v, tmp_a, cached):
                    await send_reply(media_matcher, event, "❌ 音视频合成失败，放弃发送", 500)
                    return
            else:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _download_to, url, cached, headers),
                    plugin_config.bili_video_timeout,
                )
        except asyncio.TimeoutError:
            await send_reply(media_matcher, event, "❌ 视频下载超时了", 500)
            return
        except VideoDownloadError as e:
            # 业务性失败（超限）：文案是我们拼的白名单内容，直接回显
            await send_reply(media_matcher, event, f"❌ {e}", 500)
            return
        except Exception as e:
            logger.warning("视频下载失败 %s: %s", url, e)
            await send_reply(media_matcher, event, f"❌ 视频下载失败（{type(e).__name__}）", 500)
            return
        finally:
            # 临时轨一律清理；cached 命名文件只由 _download_to/_mix_av 自己落盘
            tmp_v.unlink(missing_ok=True)
            tmp_a.unlink(missing_ok=True)
    await _send_video_file(event, cached)


# ── 主流程 ────────────────────────────────────────────────────────────

def _xhs_card(title: str, text: str, images: list[str]) -> str:
    body = text or "（无文字内容）"
    card = (f"📮 {title}\n—————————\n{body}" if title else body)
    if len(images) > plugin_config.link_parser_max_images:
        card += f"\n（共 {len(images)} 张图，发前 {plugin_config.link_parser_max_images} 张）"
    return card


async def _deliver_note_text(event: MessageEvent, card: str, images: list[str]) -> None:
    await send_reply(media_matcher, event, card, plugin_config.reply_chunk_chars)
    await _send_images(images)


@media_matcher.handle()
async def handle_media(event: MessageEvent):
    url = extract_first_url(event.get_plaintext())
    if not url:
        return
    if not _limiter.check(
        event.group_id if isinstance(event, GroupMessageEvent) else f"u:{event.user_id}"
    ):
        logger.info("media_parser 触发限流，跳过 %s", url)
        await media_matcher.finish()

    host = urlparse(url).hostname or ""
    try:
        if host.endswith("xhslink.com") or host.endswith("xiaohongshu.com"):
            await _handle_xhs(event, url)
        elif "douyin.com" in host or "iesdouyin.com" in host:
            await _handle_douyin(event, url)
        elif host.endswith("xiaoheihe.cn"):
            await _handle_heybox(event, url)
    except FinishedException:
        # matcher.finish() 的正常退出方式，别当错误吃掉
        raise
    except Exception as e:
        logger.exception("media 解析失败 %s", url)
        await send_reply(media_matcher, event, f"❌ 解析失败了（{type(e).__name__}）", 500)
        await media_matcher.finish()


async def _handle_xhs(event: MessageEvent, url: str):
    cookie = _clean_cookie(plugin_config.xhs_cookie)
    if not cookie:
        await send_reply(
            media_matcher, event,
            "⚠️ 小红书需要登录态才能抓取，请管理员在 .env 配置 xhs_cookie",
            500,
        )
        await media_matcher.finish()
    headers = {**UA_HEADERS, "Cookie": cookie}
    real = await _resolve_xhs(url)
    m = XHS_NOTE_ID_RE.search(real)
    if not m:
        await send_reply(media_matcher, event, "❌ 没认出这是哪条小红书笔记（链接形态不认识）", 500)
        await media_matcher.finish()
    note_id = m.group(1)
    cached = _note_cache.get(f"xhs{note_id}")
    if cached is not None:
        await _deliver_cached(event, cached, headers)
        await media_matcher.finish()
    await send_notice(media_matcher, event, "⏳ 正在解析这条小红书笔记…")
    page = await _grab(real, headers)
    parsed = await asyncio.to_thread(_parse_xhs_note, page, note_id)
    if not parsed:
        await send_reply(media_matcher, event, "❌ 没提取到笔记内容（笔记可能已删除，或 cookie 过期）", 500)
        await media_matcher.finish()
    title, text, images, video = parsed
    mix = _xhs_video_streams(video) if video else None
    video_url = None
    if not mix and video:
        # 兜底：部分笔记 video.media 直接给合成后的播放地址
        video_url = ((video.get("media") or {}).get("videoUrl")) or None
    card = _xhs_card(title, text, images)
    note = MediaNote(card=card, images=images, video_url=video_url, mix=mix,
                     video_tag="xhs", video_key=note_id)
    _note_cache.set(f"xhs{note_id}", note)
    if video_url or mix:
        await send_reply(media_matcher, event, card, plugin_config.reply_chunk_chars)
        await _deliver_video(event, "xhs", note_id, video_url or "", headers, mix)
        await media_matcher.finish()
    await _deliver_note_text(event, card, images)
    await media_matcher.finish()


async def _handle_douyin(event: MessageEvent, url: str):
    cookie = _clean_cookie(plugin_config.douyin_cookie)
    if not cookie:
        await send_reply(
            media_matcher, event,
            "⚠️ 抖音需要登录态才能抓取，请管理员在 .env 配置 douyin_cookie",
            500,
        )
        await media_matcher.finish()
    headers = {**UA_HEADERS, "Cookie": cookie}
    real = await _resolve_douyin(url, headers)
    m = DY_ID_RE.search(real)
    if not m:
        await send_reply(media_matcher, event, "❌ 没认出这是哪条抖音（链接形态不认识）", 500)
        await media_matcher.finish()
    aweme_id = m.group(1)
    cached = _note_cache.get(f"dy{aweme_id}")
    if cached is not None:
        await _deliver_cached(event, cached, headers)
        await media_matcher.finish()
    await send_notice(media_matcher, event, "⏳ 正在解析这条抖音…")
    # 视频和图文（note）的分享页路径不同，依次尝试
    parsed = None
    for share_page in (
        f"https://www.iesdouyin.com/share/video/{aweme_id}/",
        f"https://www.iesdouyin.com/share/note/{aweme_id}/",
    ):
        page = await _grab(share_page, headers)
        parsed = await asyncio.to_thread(_parse_douyin, page)
        if parsed:
            break
    if not parsed:
        await send_reply(media_matcher, event, "❌ 没提取到内容（视频可能已删除，或 cookie 过期）", 500)
        await media_matcher.finish()
    desc, author, images, play_url = parsed
    head = "📮 " + (desc or "抖音视频")
    if author:
        head += f"\n👤 {author}"
    if len(images) > plugin_config.link_parser_max_images:
        head += f"\n（共 {len(images)} 张图，发前 {plugin_config.link_parser_max_images} 张）"
    if play_url:
        note = MediaNote(card=head, images=[], video_url=play_url,
                         video_tag="douyin", video_key=aweme_id)
        _note_cache.set(f"dy{aweme_id}", note)
        await send_reply(media_matcher, event, head, plugin_config.reply_chunk_chars)
        await _deliver_video(event, "douyin", aweme_id, play_url, headers)
        await media_matcher.finish()
    if not images:
        await send_reply(media_matcher, event, "❌ 这条抖音没提取到内容（可能需要更新 cookie）", 500)
        await media_matcher.finish()
    note = MediaNote(card=head, images=images)
    _note_cache.set(f"dy{aweme_id}", note)
    await _deliver_note_text(event, head, images)
    await media_matcher.finish()


# ── 小黑盒（免 cookie，link/tree 接口 + hkey 签名）─────────────────────

_HEYBOX_TABLE = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _A3(e: int) -> int:
    # JS: e&128 ? (e<<1^27)&255 : e<<1 —— else 分支不掩码，按 JS 32 位位运算语义
    return ((e << 1) ^ 27) & 255 if e & 128 else (e << 1)


def _Dc(e: int) -> int:
    return _A3(e) ^ e


def _Sf(e: int) -> int:
    return _Dc(_A3(e))


def _Fh(e: int) -> int:
    return _Sf(_Dc(_A3(e)))


def _dg(e: int) -> int:
    return _Fh(e) ^ _Sf(e) ^ _Dc(e)


def _lP(s: str, tbl: str, n: int) -> str:
    o = tbl[:n]
    return "".join(tbl[ord(ch) % len(o)] for ch in s)


def _uP(s: str, tbl: str) -> str:
    return "".join(tbl[ord(ch) % len(tbl)] for ch in s)


def _J_e(strs: list[str]) -> str:
    out = ""
    for n in range(max(len(s) for s in strs)):
        for s in strs:
            if n < len(s):
                out += s[n]
    return out


def _heybox_hkey(path: str, t: int, nonce: str) -> str:
    """网页版 JS 逆向的 hkey 签名：cP.g(path, _time, nonce) = Tr(path, _time+1, nonce)。

    步骤：路径/时间/随机数按字符映射到固定字符表，交错取 20 位取 MD5，
    MD5 尾 6 字符做 AES MixColumns 变换后与原后 2 位一起求和 %100 做校验位，
    MD5 前 5 字符再映射成 5 位前缀，前缀+校验位共 7 位即 hkey。
    """
    t = t + 1
    norm = "/" + "/".join(p for p in path.split("/") if p) + "/"
    mixed = _J_e([_lP(str(t), _HEYBOX_TABLE, -2), _uP(norm, _HEYBOX_TABLE), _uP(nonce, _HEYBOX_TABLE)])[:20]
    digest = hashlib.md5(mixed.encode()).hexdigest()
    tail = [ord(c) for c in digest[-6:]]
    mixed_tail = [
        _dg(tail[0]) ^ _Fh(tail[1]) ^ _Sf(tail[2]) ^ _Dc(tail[3]),
        _Dc(tail[0]) ^ _dg(tail[1]) ^ _Fh(tail[2]) ^ _Sf(tail[3]),
        _Sf(tail[0]) ^ _Dc(tail[1]) ^ _dg(tail[2]) ^ _Fh(tail[3]),
        _Fh(tail[0]) ^ _Sf(tail[1]) ^ _Dc(tail[2]) ^ _dg(tail[3]),
        tail[4], tail[5],
    ]
    check = str(sum(mixed_tail) % 100)
    return _lP(digest[:5], _HEYBOX_TABLE, -4) + (check if len(check) >= 2 else "0" + check)


def _heybox_params(api_path: str, **extra) -> dict:
    t = int(time_mod.time())
    nonce = hashlib.md5(
        (str(t) + str(time_mod.time_ns()) + str(random.random())).encode()
    ).hexdigest().upper()
    params = {
        "app": "heybox", "os_type": "web", "x_app": "heybox_website",
        "x_client_type": "web", "x_os_type": "Windows", "x_client_version": "",
        "client_type": "web", "web_version": "3.0", "version": "999.0.4",
        "hkey": _heybox_hkey(api_path, t, nonce), "_time": t, "nonce": nonce,
    }
    params.update(extra)
    return params


def _parse_heybox_link(link: dict) -> tuple[str, list[str]] | None:
    """link/tree 的 link 对象 → (文字卡片, 图片原图列表)。

    text 是富文本块 JSON 串（type=text/img），图片 URL 的 imageMogr2 缩放
    参数去掉就是原图。
    """
    title = sanitize_one_line((link.get("title") or "").strip())
    author = sanitize_one_line(((link.get("user") or {}).get("username") or "").strip())
    ip = (link.get("ip_location") or "").strip()
    favour = link.get("favour_count") or 0
    comments = link.get("comment_num") or 0
    head = "📮 " + (title or "小黑盒帖子")
    if author:
        head += f"\n👤 {author}" + (f"（{ip}）" if ip else "")
    try:
        blocks = json.loads(link.get("text") or "[]")
    except ValueError:
        # 视频帖的 text 是纯文本，不是富文本块 JSON
        raw = (link.get("text") or "").strip()
        blocks = [{"type": "text", "text": raw}] if raw else []
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    images = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "img":
            src = (b.get("url") or "").split("?")[0]
            if src.startswith("http") and src not in images:
                images.append(src)
    body = "\n".join(t for t in texts if t.strip()).strip()
    if not body and not images:
        return None
    card = f"{head}\n—————————\n{body}" if body else head
    card += f"\n👍 {favour} · 💬 {comments}"
    return card, images


async def _handle_heybox(event: MessageEvent, url: str):
    """小黑盒帖子：SPA 页面无 SSR，直接调 api.xiaoheihe.cn 的 link/tree 接口。"""
    m = HEYBOX_LINK_ID_RE.search(url)
    if not m:
        await send_reply(media_matcher, event, "❌ 没认出这是哪条小黑盒帖子（链接形态不认识）", 500)
        await media_matcher.finish()
    link_id = m.group(1)
    cached = _note_cache.get(f"hb{link_id}")
    if cached is not None:
        await _deliver_cached(event, cached, UA_HEADERS)
        await media_matcher.finish()
    await send_notice(media_matcher, event, "⏳ 正在解析这条小黑盒帖子…")
    api_path = "/bbs/app/link/tree"
    params = _heybox_params(
        api_path, link_id=link_id, is_first="1", page="1", index="1", limit="20", owner_only="0",
    )
    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers={**UA_HEADERS, "Referer": "https://xiaoheihe.cn/"},
        ) as client:
            resp = await client.get(f"https://api.xiaoheihe.cn{api_path}", params=params)
        body = resp.json()
    except Exception as e:
        logger.warning("小黑盒接口失败 %s: %s", url, e)
        await send_reply(media_matcher, event, f"❌ 小黑盒接口请求失败（{type(e).__name__}）", 500)
        await media_matcher.finish()
    if str(body.get("status")) == "show_captcha":
        # 短时间高频请求会触发验证码风控，正常限流频率下不该出现
        logger.warning("小黑盒触发验证码风控 link_id=%s", link_id)
        await send_reply(media_matcher, event, "❌ 小黑盒风控了，过几分钟再试这条", 500)
        await media_matcher.finish()
    if str(body.get("status")) != "ok":
        logger.warning("小黑盒接口返回异常：%s", str(body.get("msg"))[:100])
        await send_reply(media_matcher, event, "❌ 小黑盒接口拒绝了请求（可能签名机制变了）", 500)
        await media_matcher.finish()
    link = (body.get("result") or {}).get("link") or {}
    if link.get("has_video") and link.get("video_url"):
        # 视频帖：文字卡片 + 下载发送（video_url 带 auth 时效签名，缓存文件不受影响）
        parsed = _parse_heybox_link(link)
        card = parsed[0] if parsed else "📮 小黑盒视频帖"
        note = MediaNote(card=card, video_url=link["video_url"],
                         video_tag="heybox", video_key=link_id)
        _note_cache.set(f"hb{link_id}", note)
        await send_reply(media_matcher, event, card, plugin_config.reply_chunk_chars)
        await _deliver_video(event, "heybox", link_id, link["video_url"],
                             {**UA_HEADERS, "Referer": "https://xiaoheihe.cn/"})
        await media_matcher.finish()
    parsed = _parse_heybox_link(link)
    if not parsed:
        await send_reply(media_matcher, event, "❌ 没提取到帖子内容（帖子可能已删除）", 500)
        await media_matcher.finish()
    card, images = parsed
    _note_cache.set(f"hb{link_id}", MediaNote(card=card, images=images))
    await _deliver_note_text(event, card, images)
    await media_matcher.finish()
