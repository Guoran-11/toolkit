"""快手适配器：短链展开 → 分享页 ``__APOLLO_STATE__`` / ``og:video`` → yt-dlp 兜底。

快手分享页的元数据主要藏在 ``window.__APOLLO_STATE__``（Apollo 缓存，键名形如
``VisionVideoDetailPhoto:xxx``）或 ``window.__INITIAL_STATE__`` 中；部分移动端页面
只保留 ``og:video`` / ``og:title``。

合规：只取平台返回的播放地址（``photoUrl`` / ``manifest`` / ``srcNoMark``），不做
任何去水印或签名破解；``srcNoMark`` 若平台未返回就使用普通地址，并在 ``note`` 中
如实标注「以平台实际提供的地址为准」。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from app.core.errors import ItemNotFoundError, LoginRequiredError, VideoParseError
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

_PHOTO_ID_RE: re.Pattern[str] = re.compile(r"(?:short-video|photo|video)/([A-Za-z0-9_-]{6,})")
_PHOTO_QUERY_RE: re.Pattern[str] = re.compile(r"[?&]photoId=([A-Za-z0-9_-]{6,})")


def _photo_id_from_url(url: str) -> str | None:
    """从快手 URL 中提取 photoId。

    Args:
        url: 作品链接。

    Returns:
        photoId；无法识别返回 None。
    """
    for pattern in (_PHOTO_ID_RE, _PHOTO_QUERY_RE):
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def _canonical_url(photo_id: str) -> str:
    """构造快手作品标准长链。

    Args:
        photo_id: 作品 ID。

    Returns:
        标准长链。
    """
    return f"https://www.kuaishou.com/short-video/{photo_id}"


def _extract_photo(payload: Any) -> dict[str, Any] | None:
    """在 Apollo / INITIAL_STATE 结构中定位作品主字典。

    Args:
        payload: 已解析的 JSON 结构。

    Returns:
        作品字典；未找到返回 None。
    """
    candidates: list[dict[str, Any]] = []
    photo_markers = (
        "caption",
        "manifest",
        "photoUrl",
        "srcNoMark",
        "coverUrls",
        "imageUrls",
        "userName",
    )
    for candidate in iter_dicts(payload):
        if not any(key in candidate for key in ("photoId", "photo_id")):
            continue
        # ``__vars`` 之类的错误上下文也带 photoId，但它没有作品内容字段，不是作品本体
        if ("errorMsg" in candidate or "result" in candidate) and not any(
            marker in candidate for marker in photo_markers
        ):
            continue
        candidates.append(candidate)
    for candidate in candidates:
        if any(marker in candidate for marker in photo_markers):
            return candidate
    return None


def _duration_seconds(photo: dict[str, Any]) -> int:
    """把快手时长换算为秒（接口通常给毫秒）。

    Args:
        photo: 作品字典。

    Returns:
        秒数。
    """
    raw = as_int(photo.get("duration")) or 0
    if raw > 1000:
        return raw // 1000
    return raw


def _manifest_representations(photo: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 ``manifest``（可能是 JSON 字符串）里的清晰度列表。

    Args:
        photo: 作品字典。

    Returns:
        representation 字典列表（按高度降序）。
    """
    manifest = photo.get("manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            manifest = None
    if not isinstance(manifest, dict):
        return []

    representations: list[dict[str, Any]] = []
    adaptations = manifest.get("adaptationSet") or manifest.get("adaptation_set")
    if isinstance(adaptations, list):
        for adaptation in adaptations:
            if not isinstance(adaptation, dict):
                continue
            items = adaptation.get("representation") or adaptation.get("representations")
            if isinstance(items, list):
                representations.extend(item for item in items if isinstance(item, dict))
    if not representations:
        items = manifest.get("representation") or manifest.get("representations")
        if isinstance(items, list):
            representations.extend(item for item in items if isinstance(item, dict))

    def _height(entry: dict[str, Any]) -> int:
        """取 representation 高度。

        Args:
            entry: representation 字典。

        Returns:
            高度像素。
        """
        return as_int(entry.get("height")) or 0

    representations.sort(key=_height, reverse=True)
    return representations


def _build_formats(photo: dict[str, Any], duration_sec: int) -> list[VideoFormat]:
    """把 manifest / photoUrl / srcNoMark 映射为统一格式列表。

    Args:
        photo: 作品字典。
        duration_sec: 时长（秒）。

    Returns:
        VideoFormat 列表。
    """
    formats: list[VideoFormat] = []
    for index, entry in enumerate(_manifest_representations(photo)):
        url = first_url(entry.get("url") or entry.get("Url") or entry.get("src"))
        if not url:
            continue
        height = as_int(entry.get("height"))
        width = as_int(entry.get("width"))
        fps = as_int(entry.get("frameRate") or entry.get("frame_rate")) or 30
        bitrate = as_int(entry.get("avgBitrate") or entry.get("avg_bitrate") or entry.get("bitrate"))
        quality = normalize_quality(height, fps)
        formats.append(
            VideoFormat(
                format_id=as_text(entry.get("qualityLabel") or entry.get("quality_label"), 32)
                or f"v-{index}",
                quality=quality,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                container="mp4",
                size_bytes=int(bitrate * duration_sec / 8) if bitrate and duration_sec else None,
                note="以平台实际提供的地址为准",
                url=url,
            )
        )

    if formats:
        return formats

    # 单地址兜底：优先平台返回的不带水印地址（若存在），否则普通播放地址
    url = (
        first_url(photo.get("srcNoMark"))
        or first_url(photo.get("photoUrl"))
        or first_url(photo.get("playUrl"))
        or first_url(photo.get("src"))
    )
    if not url:
        return formats
    height = as_int(photo.get("height"))
    width = as_int(photo.get("width"))
    formats.append(
        VideoFormat(
            format_id="photo",
            quality=normalize_quality(height, 30),
            width=width,
            height=height,
            fps=30,
            container="mp4",
            note="以平台实际提供的地址为准",
            url=url,
        )
    )
    return formats


def _build_images(photo: dict[str, Any]) -> list[Image]:
    """提取图文/图集作品的图片列表。

    Args:
        photo: 作品字典。

    Returns:
        Image 列表。
    """
    raw_images = photo.get("images") or photo.get("imageUrls") or photo.get("atlas")
    if isinstance(raw_images, dict):
        raw_images = raw_images.get("list") or raw_images.get("images")
    if not isinstance(raw_images, list):
        return []
    images: list[Image] = []
    for entry in raw_images:
        url = first_url(entry)
        if not url:
            continue
        width = as_int(entry.get("width")) if isinstance(entry, dict) else None
        height = as_int(entry.get("height")) if isinstance(entry, dict) else None
        images.append(Image(url=url, width=width, height=height))
    return images


def _item_from_photo(photo: dict[str, Any], url: str, parse_source: str) -> VideoItem:
    """把作品字典映射为 VideoItem。

    Args:
        photo: 作品字典。
        url: 请求 URL。
        parse_source: api / html / ytdlp。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 既无播放地址也无图片。
    """
    photo_id = as_text(photo.get("photoId") or photo.get("photo_id"), 64) or (
        _photo_id_from_url(url) or ""
    )
    if not photo_id:
        raise VideoParseError("未能识别快手作品 ID（页面结构可能已调整）")

    duration_sec = _duration_seconds(photo)
    formats = _build_formats(photo, duration_sec)
    images = _build_images(photo)
    if images and formats:
        media_type = "mix"
    elif images:
        media_type = "image"
    else:
        media_type = "video"
    if not formats and not images:
        if photo.get("memberOnly") or photo.get("loginRequired") or photo.get("workType") == 1:
            raise LoginRequiredError(
                "该作品需要登录或为会员专属，请在设置中填写快手 Cookie（会员专属内容不支持下载）"
            )
        raise VideoParseError("快手页面未返回可用的播放地址或图片（作品可能受限或需要登录）")

    user = photo.get("user") if isinstance(photo.get("user"), dict) else {}
    caption = as_text(photo.get("caption") or photo.get("title"), 2000)
    cover = (
        first_url(photo.get("coverUrls"))
        or first_url(photo.get("coverUrl"))
        or first_url(photo.get("cover"))
        or (images[0].url if images else None)
    )
    stats_raw = photo.get("stats") if isinstance(photo.get("stats"), dict) else photo
    stats = {
        "like": as_int(stats_raw.get("likeCount") or stats_raw.get("like_count")) or 0,
        "comment": as_int(stats_raw.get("commentCount") or stats_raw.get("comment_count")) or 0,
        "share": as_int(stats_raw.get("shareCount") or stats_raw.get("share_count")) or 0,
        "play": as_int(stats_raw.get("viewCount") or stats_raw.get("view_count")) or 0,
    }
    if not any(stats.values()):
        stats = None

    return VideoItem(
        platform="kuaishou",
        item_id=photo_id,
        url=_canonical_url(photo_id),
        media_type=media_type,
        title=as_text(caption.splitlines()[0] if caption else "", 200) or "未命名作品",
        description=caption,
        author=Author(
            name=as_text(photo.get("userName") or user.get("name") or photo.get("authorName"), 100)
            or "未知作者",
            id=as_text(photo.get("userId") or user.get("id") or photo.get("authorId"), 64) or None,
            avatar_url=first_url(photo.get("headUrl") or user.get("headUrl") or user.get("avatar")),
        ),
        cover_url=cover,
        duration_sec=duration_sec,
        published_at=to_iso(as_int(photo.get("timestamp") or photo.get("createTime"))),
        stats=stats,
        formats=formats,
        images=images,
        piece_count=1,
        warnings=[],
        parse_source=parse_source,
    )


def _item_from_meta(html: str, url: str) -> VideoItem:
    """``og:*`` 元信息兜底。

    Args:
        html: 分享页 HTML。
        url: 当前 URL。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 元信息不足。
    """
    meta = parse_meta_tags(html)
    photo_id = _photo_id_from_url(url) or _photo_id_from_url(meta.get("og:url", "")) or ""
    title = as_text(meta.get("og:title") or meta.get("twitter:title"), 200) or "未命名作品"
    cover = meta.get("og:image") or meta.get("twitter:image")
    video_url = meta.get("og:video:url") or meta.get("og:video") or meta.get("og:video:secure_url")
    formats: list[VideoFormat] = []
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
                note="以平台实际提供的地址为准",
                url=video_url,
            )
        )
    if not photo_id or (not formats and not cover):
        raise VideoParseError("快手页面结构已变化，未能解析出作品信息")
    return VideoItem(
        platform="kuaishou",
        item_id=photo_id,
        url=_canonical_url(photo_id),
        media_type="video" if formats else "image",
        title=title,
        description=title,
        author=Author(name="未知作者"),
        cover_url=cover,
        duration_sec=0,
        formats=formats,
        parse_source="html",
    )


def _missing_detail(payload: Any) -> str | None:
    """从失效响应中提取「作品不存在」的说明。

    快手失效作品返回 ``defaultClient={}`` + ``__vars.errorMsg``；映射为更具体的
    ``ITEM_NOT_FOUND``（契约 §4.1 错误码优先级）。

    Args:
        payload: 已解析的 JSON 结构。

    Returns:
        错误说明；不是失效场景返回 None。
    """
    for node in iter_dicts(payload):
        client = node.get("defaultClient")
        if isinstance(client, dict) and not client:
            variables = node.get("__vars")
            if isinstance(variables, dict):
                message = as_text(variables.get("errorMsg"), 200)
                if message:
                    return message
            return "作品不存在或已删除"
        variables = node.get("__vars")
        if isinstance(variables, dict) and as_int(variables.get("result")) == 2:
            return as_text(variables.get("errorMsg"), 200) or "作品不存在或已删除"
    return None


def parse_fixture(raw: str | bytes | dict[str, Any], url: str) -> VideoItem:
    """离线解析快手分享页/接口原始内容（不触网，供夹具测试使用）。

    Args:
        raw: 页面 HTML、JSON 文本或已解析的字典。
        url: 作品 URL。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 无法解析。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    if isinstance(raw, dict):
        photo = _extract_photo(raw)
        if photo is None:
            detail = _missing_detail(raw)
            if detail:
                raise ItemNotFoundError(detail)
            raise VideoParseError("快手数据中未找到作品对象")
        return _item_from_photo(photo, url, "api")

    text = str(raw)
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            photo = _extract_photo(payload)
            if photo is not None:
                return _item_from_photo(photo, url, "api")
            detail = _missing_detail(payload)
            if detail:
                raise ItemNotFoundError(detail)

    payload = extract_script_json(text)
    if payload is not None:
        photo = _extract_photo(payload)
        if photo is not None:
            return _item_from_photo(photo, url, "html")
        detail = _missing_detail(payload)
        if detail:
            raise ItemNotFoundError(detail)

    if "<html" in text.lower() or "<meta" in text.lower():
        return _item_from_meta(text, url)

    raise VideoParseError("快手页面结构已变化，未能解析出作品信息")


class KuaishouAdapter:
    """快手平台适配器。"""

    name = "kuaishou"
    hosts = tuple(registry.PLATFORMS["kuaishou"])

    def match(self, url: str) -> bool:
        """判断 URL 是否属于快手。

        Args:
            url: 目标 URL。

        Returns:
            是否匹配。
        """
        return registry.platform_for_url(url) == self.name

    def normalize(self, url: str) -> str:
        """展开短链。

        Args:
            url: 原始 URL。

        Returns:
            长链或原 URL。
        """
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host in registry.SHORTLINK_HOSTS:
            return registry.expand_shortlink(url)
        return url

    def _fetch(self, url: str, ctx: ParseContext) -> str:
        """抓取快手分享页。

        Args:
            url: 分享页 URL。
            ctx: 解析上下文。

        Returns:
            页面文本。

        Raises:
            ItemNotFoundError: 404。
            VideoParseError: 其它抓取失败。
        """
        headers = {
            "User-Agent": net.UA_MOBILE,
            "Referer": "https://www.kuaishou.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            _final, text, status = net.http_get_text(
                url, timeout=ctx.timeout, headers=headers, max_redirects=3
            )
        except net.NetError as exc:
            raise VideoParseError("快手页面抓取失败，请检查网络后重试") from exc
        if status == 404:
            raise ItemNotFoundError("作品不存在或已被作者删除/设为私密")
        if status >= 400:
            raise VideoParseError(f"快手返回了异常状态（HTTP {status}），请稍后再试")
        return text

    def parse(self, url: str, ctx: ParseContext) -> VideoItem:
        """抓取并解析快手作品。

        Args:
            url: 作品链接。
            ctx: 解析上下文。

        Returns:
            VideoItem。

        Raises:
            ItemNotFoundError: 作品不存在。
            VideoParseError: 解析失败且兜底链耗尽。
        """
        normalized = self.normalize(url)
        text = self._fetch(normalized, ctx)
        try:
            item = parse_fixture(text, normalized)
        except VideoParseError as exc:
            from app.modules.video.adapters import ytdlp

            fallback = ytdlp.parse(normalized, self.name, ctx)
            if fallback is not None:
                return fallback
            raise exc
        return item


#: 模块级单例（registry.get_adapter 依赖该属性名）
ADAPTER = KuaishouAdapter()


__all__ = ["ADAPTER", "KuaishouAdapter", "parse_fixture"]
