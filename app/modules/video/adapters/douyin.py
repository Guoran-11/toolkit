"""抖音适配器：短链展开 → 分享页内嵌 JSON / og:* → yt-dlp 兜底。

解析策略（契约 §4.1 降级链）：
    1. 分享页 HTML 里的 ``window._ROUTER_DATA`` / ``window.RENDER_DATA`` /
       ``window.__INITIAL_STATE__``（移动端 UA 下命中率更高）；
    2. 同上，但**带匿名访客 Cookie（ttwid）**再抓一次——2026-10 起
       ``/share/video/{id}/`` 对普通 UA 不再内嵌作品 JSON，只有带 ttwid 时
       ``loaderData["video_(id)/page"].videoInfoRes.item_list`` 才会出现；
    3. ``og:video`` / ``og:title`` / ``og:image`` 元信息兜底；
    4. 可选 yt-dlp 引擎；
    5. 仍失败抛 ``VideoParseError``（页面结构可能已调整）。

关于 ttwid 的合规说明：``ttwid`` 是字节跳动下发的**匿名访客标识**，通过平台公开的
注册接口获取，无需账号登录、不含用户数据、也不做任何签名伪造；它只用于让分享页
返回「本来就公开可见」的作品数据（不涉及私密/付费内容）。与用户手动配置的登录
Cookie 是两回事：后者仍由 ``ctx.cookie`` 原样透传，本模块既不读取也不记录其内容。

合规：只读取平台对外返回的播放地址，不做去水印、不绕过登录/付费；地址里带
``playwm`` 时在 ``note`` 中如实标注「可能含水印」。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any

from app.core.errors import ItemNotFoundError, LoginRequiredError, ToolkitError, VideoParseError
from app.modules.video import net
from app.modules.video.adapters import registry
from app.modules.video.adapters.base import (
    ParseContext,
    as_int,
    as_text,
    extract_script_json,
    first_url,
    iter_dicts,
    normalize_quality,
    parse_meta_tags,
    to_iso,
)
from app.modules.video.models import Author, Image, VideoFormat, VideoItem

logger = logging.getLogger(__name__)

_AWEME_ID_RE: re.Pattern[str] = re.compile(r"(?:video|note|slides)/(\d{6,})")
_MODAL_ID_RE: re.Pattern[str] = re.compile(r"[?&]modal_id=(\d{6,})")

# ---------------------------------------------------------------- 匿名访客 ID
#
# 平台公开的访客注册接口：POST 一个固定的 JSON，响应 ``Set-Cookie`` 里带
# ``ttwid=1%7C...``。实测（2026-10-21）该 Cookie 是分享页返回作品数据的必要条件，
# 有效期约 1 年；这里按 6 小时缓存，避免每次解析都多打一次接口。
_TTWID_REGISTER_URL: str = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_REGISTER_BODY: dict[str, Any] = {
    "region": "cn",
    "aid": 1768,
    "needFid": False,
    "service": "www.ixigua.com",
    "migrate_info": {"ticket": "", "source": "node"},
    "cbUrlProtocol": "https",
    "union": True,
}
_TTWID_REGISTER_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
}
#: 进程内缓存 TTL（秒）
_TTWID_TTL_SEC: float = 6 * 60 * 60
#: 从 ``Set-Cookie`` 里取 ``ttwid`` 值（到分号/逗号/空白为止）
_TTWID_COOKIE_RE: re.Pattern[str] = re.compile(r"(?:^|[;,\s])ttwid=([^;,\s]+)")

#: ``(过期 monotonic 时间, cookie 值)``；值为 None 表示尚未获取
_ttwid_cache: tuple[float, str] | None = None
_ttwid_lock = threading.Lock()


def _parse_ttwid_cookie(set_cookie: str) -> str | None:
    """从 ``Set-Cookie`` 响应头里取出 ``ttwid=...`` 这一段（到分号为止）。

    Args:
        set_cookie: ``Set-Cookie`` 头原文（可能含 Path/Domain/Max-Age 等属性）。

    Returns:
        可直接放进 ``Cookie`` 请求头的 ``ttwid=<值>``；未命中返回 None。
    """
    if not set_cookie:
        return None
    match = _TTWID_COOKIE_RE.search(set_cookie)
    if not match:
        return None
    value = match.group(1).strip()
    return f"ttwid={value}" if value else None


def _get_ttwid(timeout: float) -> str | None:
    """获取匿名访客 Cookie（``ttwid=...``），带进程内缓存。

    命中缓存直接返回；未命中时调用平台公开的访客注册接口，并从响应
    ``Set-Cookie`` 里解析出 ``ttwid``。任何失败都静默返回 None（不影响主链路，
    也不抛异常），且**绝不记录 Cookie 值**。

    Args:
        timeout: 单次请求超时秒数。

    Returns:
        ``ttwid=<值>`` 形式的 Cookie 串（可直接作为 ``Cookie`` 请求头）；
        获取失败返回 None。
    """
    global _ttwid_cache

    now = time.monotonic()
    with _ttwid_lock:
        cached = _ttwid_cache
        if cached is not None and cached[0] > now:
            return cached[1]

    try:
        _final, _payload, status, response_headers = net.http_post_json(
            _TTWID_REGISTER_URL,
            json_body=_TTWID_REGISTER_BODY,
            timeout=timeout,
            headers=_TTWID_REGISTER_HEADERS,
            max_redirects=2,
        )
    except (net.NetError, ToolkitError) as exc:
        # 只记异常类型：响应体/请求头都可能含 Cookie，绝不落日志
        logger.debug("访客 ID 注册接口调用失败（已降级为无 Cookie 解析）：%s", type(exc).__name__)
        return None

    if not 200 <= status < 300:
        logger.debug("访客 ID 注册接口返回 HTTP %s（已降级为无 Cookie 解析）", status)
        return None

    cookie = _parse_ttwid_cookie(response_headers.get("set-cookie", ""))
    if cookie is None:
        logger.debug("访客 ID 注册接口未返回 ttwid（已降级为无 Cookie 解析）")
        return None

    with _ttwid_lock:
        _ttwid_cache = (time.monotonic() + _TTWID_TTL_SEC, cookie)
    return cookie


def _aweme_id_from_url(url: str) -> str | None:
    """从抖音 URL 中提取作品 ID。

    Args:
        url: 长链/短链展开后的 URL。

    Returns:
        aweme_id；无法识别返回 None。
    """
    for pattern in (_AWEME_ID_RE, _MODAL_ID_RE):
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def _canonical_url(aweme_id: str, media_type: str) -> str:
    """构造作品标准长链。

    Args:
        aweme_id: 作品 ID。
        media_type: video / image / mix。

    Returns:
        标准长链。
    """
    kind = "note" if media_type == "image" else "video"
    return f"https://www.douyin.com/{kind}/{aweme_id}"


def _duration_seconds(video: dict[str, Any]) -> int:
    """把抖音的视频时长换算为秒。

    抖音 ``video.duration`` 单位是毫秒；部分第三方接口返回秒。这里用数量级区分：
    超过 1000 视为毫秒。

    Args:
        video: 视频字典。

    Returns:
        秒数（非负）。
    """
    raw = as_int(video.get("duration")) or 0
    if raw > 1000:
        return raw // 1000
    return raw


def _extract_aweme(payload: Any) -> dict[str, Any] | None:
    """在任意嵌套结构中定位作品主字典。

    Args:
        payload: 已解析的 JSON 结构。

    Returns:
        作品字典；未找到返回 None。
    """
    candidates: list[dict[str, Any]] = []
    for candidate in iter_dicts(payload):
        if "aweme_id" not in candidate and "awemeId" not in candidate:
            continue
        # filter_list 里的条目也带 aweme_id，但它只是「被过滤原因」，不是作品本体
        if "filter_reason" in candidate or "detail_msg" in candidate:
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    # 优先含 video / images 的字典（最内层才是真正的作品对象）
    for candidate in candidates:
        if "video" in candidate or "images" in candidate:
            return candidate
    return candidates[0]


def _extract_author(aweme: dict[str, Any]) -> Author:
    """提取作者信息。

    Args:
        aweme: 作品字典。

    Returns:
        Author 对象。
    """
    raw = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    avatar = (
        first_url(raw.get("avatar_thumb"))
        or first_url(raw.get("avatar_medium"))
        or first_url(raw.get("avatar_larger"))
    )
    return Author(
        name=as_text(raw.get("nickname") or raw.get("name"), 100) or "未知作者",
        id=as_text(raw.get("sec_uid") or raw.get("uid"), 200) or None,
        avatar_url=avatar,
    )


def _build_formats(video: dict[str, Any], duration_sec: int) -> list[VideoFormat]:
    """把 ``video.bit_rate[]`` / ``play_addr`` 映射为统一格式列表。

    Args:
        video: 作品里的 video 字典。
        duration_sec: 视频时长（秒），用于估算体积。

    Returns:
        VideoFormat 列表；无播放地址时返回空列表。
    """
    formats: list[VideoFormat] = []
    seen: set[str] = set()
    bit_rates = video.get("bit_rate") or video.get("bitRate") or []
    if not isinstance(bit_rates, list):
        bit_rates = []

    for index, entry in enumerate(bit_rates):
        if not isinstance(entry, dict):
            continue
        play = entry.get("play_addr") or entry.get("playAddr") or {}
        if not isinstance(play, dict):
            play = {}
        url = first_url(play)
        if not url or url in seen:
            continue
        seen.add(url)
        height = as_int(play.get("height")) or as_int(video.get("height"))
        width = as_int(play.get("width")) or as_int(video.get("width"))
        fps = as_int(entry.get("fps")) or as_int(video.get("fps")) or 30
        bitrate = as_int(entry.get("bit_rate")) or as_int(video.get("bitrate"))
        size = as_int(play.get("data_size"))
        if size is None and bitrate and duration_sec:
            size = int(bitrate * duration_sec / 8)
        formats.append(
            VideoFormat(
                format_id=str(entry.get("gear_name") or f"v-{index}"),
                quality=normalize_quality(height, fps),
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                container="mp4",
                size_bytes=size,
                need_login=False,
                need_vip=False,
                note="平台播放地址（可能含水印）" if "playwm" in url else None,
                url=url,
            )
        )

    if formats:
        formats.sort(key=lambda fmt: fmt.height or 0, reverse=True)
        return formats

    play = video.get("play_addr") or video.get("playAddr") or {}
    if isinstance(play, dict):
        url = first_url(play)
        if url:
            height = as_int(play.get("height")) or as_int(video.get("height"))
            width = as_int(play.get("width")) or as_int(video.get("width"))
            fps = as_int(video.get("fps")) or 30
            bitrate = as_int(video.get("bitrate"))
            size = as_int(play.get("data_size"))
            if size is None and bitrate and duration_sec:
                size = int(bitrate * duration_sec / 8)
            formats.append(
                VideoFormat(
                    format_id="play",
                    quality=normalize_quality(height, fps),
                    width=width,
                    height=height,
                    fps=fps,
                    bitrate=bitrate,
                    container="mp4",
                    size_bytes=size,
                    note="平台播放地址（可能含水印）" if "playwm" in url else None,
                    url=url,
                )
            )
    return formats


def _build_images(aweme: dict[str, Any]) -> list[Image]:
    """提取图文作品的图片列表。

    Args:
        aweme: 作品字典。

    Returns:
        Image 列表。
    """
    raw_images = aweme.get("images")
    if not isinstance(raw_images, list):
        return []
    images: list[Image] = []
    for entry in raw_images:
        if not isinstance(entry, dict):
            continue
        url = first_url(entry)
        if not url:
            continue
        images.append(
            Image(
                url=url,
                width=as_int(entry.get("width")),
                height=as_int(entry.get("height")),
            )
        )
    return images


def _stats_of(aweme: dict[str, Any]) -> dict[str, int] | None:
    """提取互动数据并映射为统一字段名。

    Args:
        aweme: 作品字典。

    Returns:
        ``{like, comment, share, play}``；无数据返回 None。
    """
    raw = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
    if not raw:
        return None
    stats = {
        "like": as_int(raw.get("digg_count")) or 0,
        "comment": as_int(raw.get("comment_count")) or 0,
        "share": as_int(raw.get("share_count")) or 0,
        "play": as_int(raw.get("play_count")) or 0,
    }
    return stats


def _item_from_aweme(aweme: dict[str, Any], url: str, parse_source: str) -> VideoItem:
    """把作品字典映射为 :class:`VideoItem`。

    Args:
        aweme: 作品字典。
        url: 请求 URL（用于兜底 ID 与长链）。
        parse_source: api / html / ytdlp。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 既无视频地址也无图片。
    """
    item_id = as_text(aweme.get("aweme_id") or aweme.get("awemeId"), 64) or (_aweme_id_from_url(url) or "")
    if not item_id:
        raise VideoParseError("未能识别抖音作品 ID（页面结构可能已调整）")

    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}
    duration_sec = _duration_seconds(video)
    formats = _build_formats(video, duration_sec)
    images = _build_images(aweme)

    description = as_text(aweme.get("desc") or aweme.get("title"), 2000)
    title = as_text(description.splitlines()[0] if description else "", 200) or "未命名作品"
    cover = (
        first_url(video.get("cover"))
        or first_url(video.get("origin_cover"))
        or first_url(video.get("dynamic_cover"))
        or (images[0].url if images else None)
    )

    if images and formats:
        media_type = "mix"
    elif images:
        media_type = "image"
    else:
        media_type = "video"

    if not formats and not images:
        status_raw = aweme.get("status") if isinstance(aweme.get("status"), dict) else {}
        if as_int(status_raw.get("is_delete")) or as_int(aweme.get("is_delete")):
            raise ItemNotFoundError("作品不存在或已被作者删除")
        play = video.get("play_addr") or video.get("playAddr")
        empty_play = isinstance(play, dict) and not first_url(play)
        login_hint = (
            empty_play
            or as_int(status_raw.get("private_status")) == 1
            or bool(status_raw.get("is_prohibited"))
            or bool(status_raw.get("is_reviewing"))
            or bool(aweme.get("is_login_required"))
        )
        if login_hint:
            # 契约 §4.1：按最具体错误码返回——需登录优先于泛化的解析失败
            raise LoginRequiredError(
                "该作品需要登录后才能查看（可能为私密或受限作品），请先在设置中填写抖音 Cookie 后重试"
            )
        raise VideoParseError("抖音页面未返回可用的播放地址或图片（可能需要登录/作品受限）")

    return VideoItem(
        platform="douyin",
        item_id=item_id,
        url=_canonical_url(item_id, media_type),
        media_type=media_type,
        title=title,
        description=description,
        author=_extract_author(aweme),
        cover_url=cover,
        duration_sec=duration_sec,
        published_at=to_iso(as_int(aweme.get("create_time"))),
        stats=_stats_of(aweme),
        formats=formats,
        images=images,
        piece_count=1,
        warnings=[],
        parse_source=parse_source,
    )


def _item_from_meta(html: str, url: str) -> VideoItem:
    """og:* 元信息兜底解析。

    Args:
        html: 分享页 HTML。
        url: 当前 URL。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 元信息不足。
    """
    meta = parse_meta_tags(html)
    item_id = _aweme_id_from_url(url) or _aweme_id_from_url(meta.get("og:url", "")) or ""
    title = as_text(meta.get("og:title") or meta.get("twitter:title"), 200) or "未命名作品"
    cover = meta.get("og:image") or meta.get("twitter:image")
    formats: list[VideoFormat] = []
    video_url = meta.get("og:video:url") or meta.get("og:video") or meta.get("og:video:secure_url")
    if video_url:
        height = as_int(meta.get("og:video:height"))
        width = as_int(meta.get("og:video:width"))
        formats.append(
            VideoFormat(
                format_id="og",
                quality=normalize_quality(height, 30),
                width=width,
                height=height,
                fps=30,
                container="mp4",
                need_login=False,
                need_vip=False,
                url=video_url,
            )
        )
    if not item_id or (not formats and not cover):
        raise VideoParseError("抖音页面结构已变化，未能解析出作品信息")
    return VideoItem(
        platform="douyin",
        item_id=item_id,
        url=_canonical_url(item_id, "video"),
        media_type="video",
        title=title,
        description=title,
        author=Author(name="未知作者"),
        cover_url=cover,
        duration_sec=0,
        formats=formats,
        parse_source="html",
    )


def _missing_detail(payload: Any) -> str | None:
    """从失败响应中提取「作品不存在」的说明。

    抖音失效作品会返回 ``item_list=[]`` + ``filter_list[].detail_msg``（如
    「作品不存在或已删除」）；把它映射为更具体的 ``ITEM_NOT_FOUND``，而不是泛化的
    ``PARSE_FAILED``（契约 §4.1 错误码优先级）。

    Args:
        payload: 已解析的 JSON 结构。

    Returns:
        错误说明；不是「作品不存在」场景返回 None。
    """
    for node in iter_dicts(payload):
        filters = node.get("filter_list")
        if isinstance(filters, list) and filters:
            first = filters[0] if isinstance(filters[0], dict) else {}
            return as_text(first.get("detail_msg"), 200) or "作品不存在或已被删除/设为私密"
    return None


def parse_fixture(raw: str | bytes | dict[str, Any], url: str) -> VideoItem:
    """离线解析抖音分享页/接口原始内容（不触网，供夹具测试使用）。

    Args:
        raw: 页面 HTML、JSON 文本或已解析的字典。
        url: 作品 URL（用于提取 ID / 构造长链）。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 无法从内容中解析出作品。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    if isinstance(raw, dict):
        aweme = _extract_aweme(raw)
        if aweme is None:
            detail = _missing_detail(raw)
            if detail:
                raise ItemNotFoundError(detail)
            raise VideoParseError("抖音数据中未找到作品对象")
        return _item_from_aweme(aweme, url, "api")

    text = str(raw)
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            aweme = _extract_aweme(payload)
            if aweme is not None:
                return _item_from_aweme(aweme, url, "api")
            detail = _missing_detail(payload)
            if detail:
                raise ItemNotFoundError(detail)

    payload = extract_script_json(text)
    if payload is not None:
        aweme = _extract_aweme(payload)
        if aweme is not None:
            return _item_from_aweme(aweme, url, "html")
        detail = _missing_detail(payload)
        if detail:
            raise ItemNotFoundError(detail)

    if "<html" in text.lower() or "<meta" in text.lower():
        return _item_from_meta(text, url)

    raise VideoParseError("抖音页面结构已变化，未能解析出作品信息")


class DouyinAdapter:
    """抖音平台适配器。"""

    name = "douyin"
    hosts = tuple(registry.PLATFORMS["douyin"])

    def match(self, url: str) -> bool:
        """判断 URL 是否属于抖音。

        Args:
            url: 目标 URL。

        Returns:
            是否匹配。
        """
        return registry.platform_for_url(url) == self.name

    def normalize(self, url: str) -> str:
        """展开短链并归一化。

        Args:
            url: 原始 URL。

        Returns:
            长链。
        """
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host in registry.SHORTLINK_HOSTS:
            return registry.expand_shortlink(url)
        return url

    def _fetch(self, url: str, ctx: ParseContext, *, cookie: str | None = None) -> str:
        """抓取分享页 HTML。

        Args:
            url: 分享页 URL。
            ctx: 解析上下文。
            cookie: 额外 ``Cookie`` 请求头（用户配置的登录 Cookie 或匿名访客
                ``ttwid``）；为空时不发送。Cookie 只进请求头，绝不写日志或异常文案。

        Returns:
            页面文本。

        Raises:
            ItemNotFoundError: 页面 404（作品删除/私密）。
            VideoParseError: 其它抓取失败。
        """
        headers = {
            "User-Agent": net.UA_MOBILE,
            "Referer": "https://www.douyin.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if cookie:
            headers["Cookie"] = cookie
        try:
            _final, text, status = net.http_get_text(
                url, timeout=ctx.timeout, headers=headers, max_redirects=3
            )
        except net.NetError as exc:
            raise VideoParseError("抖音页面抓取失败，请检查网络后重试") from exc
        if status == 404:
            raise ItemNotFoundError("作品不存在或已被作者删除/设为私密")
        if status >= 400:
            raise VideoParseError(f"抖音返回了异常状态（HTTP {status}），可能触发风控，请稍后再试")
        return text

    def _parse_page(self, fetch_url: str, normalized: str, ctx: ParseContext) -> VideoItem:
        """抓取分享页并解析；无数据页时带匿名访客 Cookie 重试一次。

        2026-10 起抖音分享页对普通 UA 不再内嵌作品 JSON（``videoInfoRes`` 缺失），
        同一 URL 带上匿名访客 Cookie ``ttwid`` 后才会出现 ``item_list``。因此首次
        解析失败（``VideoParseError``）时按以下顺序处理：

            1. 取 ``ctx.cookie``（用户已配置的登录 Cookie）或自动申请的 ``ttwid``；
            2. 非空则带 Cookie 再抓一次并重新解析，成功即返回；
            3. 仍失败时抛**第二次**的 ``VideoParseError``（文案补充「平台可能要求
               访客 Cookie」），供上层继续走 yt-dlp 兜底；
            4. 第二次抓取本身失败（网络/HTTP 异常）时抛第一次的异常。

        ``ItemNotFoundError``（``filter_list`` 明确说明作品不存在/已删除）在任一
        阶段都直接向上抛，不会被重试掩盖。

        Args:
            fetch_url: 分享页 URL（``/share/video/{id}/``）。
            normalized: 归一化后的作品长链（用于解析结果与 ID 兜底）。
            ctx: 解析上下文。

        Returns:
            VideoItem。

        Raises:
            ItemNotFoundError: 作品不存在/已删除/私密。
            VideoParseError: 两次解析都失败，或第二次抓取失败。
        """
        text = self._fetch(fetch_url, ctx, cookie=ctx.cookie)
        try:
            return parse_fixture(text, normalized)
        except ItemNotFoundError:
            raise
        except VideoParseError as first_exc:
            cookie = ctx.cookie or _get_ttwid(ctx.timeout)
            if not cookie:
                # 拿不到访客 Cookie：保持原有错误语义（不额外增加请求）
                raise
            try:
                retry_text = self._fetch(fetch_url, ctx, cookie=cookie)
            except (VideoParseError, ItemNotFoundError):
                # 第二次抓取本身失败：抛第一次的解析错误（更能说明页面结构问题）
                raise first_exc from None
            try:
                return parse_fixture(retry_text, normalized)
            except ItemNotFoundError:
                raise
            except VideoParseError as second_exc:
                raise VideoParseError(
                    f"{second_exc.detail}（平台可能要求访客 Cookie：可稍后重试，"
                    "或在设置中填写抖音 Cookie）"
                ) from None

    def parse(self, url: str, ctx: ParseContext) -> VideoItem:
        """抓取并解析抖音作品。

        Args:
            url: 作品链接（可含短链）。
            ctx: 解析上下文。

        Returns:
            VideoItem。

        Raises:
            ItemNotFoundError: 作品不存在。
            VideoParseError: 解析失败且兜底链耗尽。
        """
        normalized = self.normalize(url)
        item_id = _aweme_id_from_url(normalized)
        fetch_url = (
            f"https://www.iesdouyin.com/share/video/{item_id}/"
            if item_id
            else normalized
        )
        try:
            item = self._parse_page(fetch_url, normalized, ctx)
        except ItemNotFoundError:
            raise
        except VideoParseError as exc:
            from app.modules.video.adapters import ytdlp

            fallback = ytdlp.parse(normalized, self.name, ctx)
            if fallback is not None:
                return fallback
            raise exc
        if not item.url:
            item.url = _canonical_url(item.item_id, item.media_type)
        return item


#: 模块级单例（registry.get_adapter 依赖该属性名）
ADAPTER = DouyinAdapter()


__all__ = ["ADAPTER", "DouyinAdapter", "parse_fixture"]
