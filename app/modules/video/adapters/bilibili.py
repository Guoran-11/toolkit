"""B站适配器：公开 view 接口 + playurl（DASH / durl）+ yt-dlp 兜底。

B站是三平台中接口最稳定的一家，因此优先做扎实：

    · ``/x/web-interface/view?bvid=...`` 取稿件元数据（标题/UP主/封面/时长/分P/互动数）；
    · ``/x/player/playurl?...&fnval=4048`` 取 DASH 分离流（视频 + 音频），
      兜底 ``fnval=1`` 取渐进式 mp4；
    · ``support_formats`` / ``accept_quality`` 用于标记「需登录」「会员专属」的清晰度；
    · 未配置 Cookie 时匿名可得最高清晰度通常为 720P；1080P 在 ``formats`` 里以
      ``need_login=true``、无媒体地址的形式展示，并附 ``warnings=["LOGIN_REQUIRED"]``，
      上层据此返回 ok=false + item（前端仍可降级下载 720P）。

``format_id`` 约定（必须全局唯一且跨两次解析稳定，下载任务会重新解析后按 id 回查）：
    · DASH 流 ``dash-{qn}-{codecid}``（qn 未知时 ``dash-h{height}-{codecid}``）；
    · 渐进式 durl 流 ``durl-{qn}``；
    · 无媒体地址的占位项 ``str(qn)``；兜底项 ``unknown``。
同一个 qn 的多种编码（AVC/HEVC/AV1）只保留一条（AVC 优先），占位项只在该档位
没有真实流时补出，避免同一清晰度出现多个同名项。

合规：会员专属清晰度（4K/2K）只展示、不下载，``need_vip=true`` 由上层置灰。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from app.core.errors import (
    CookieInvalidError,
    ItemNotFoundError,
    LoginRequiredError,
    RegionLockedError,
    VideoParseError,
)
from app.modules.video import net
from app.modules.video.adapters import registry
from app.modules.video.adapters.base import (
    ParseContext,
    as_int,
    as_signed_int,
    as_text,
    find_dict_with,
    first_url,
    normalize_quality,
    to_iso,
)
from app.modules.video.models import Author, VideoFormat, VideoItem

_BVID_RE: re.Pattern[str] = re.compile(r"(BV[0-9A-Za-z]{10})")
_AID_RE: re.Pattern[str] = re.compile(r"(?:/video/av|^av)(\d+)", re.IGNORECASE)

#: qn（清晰度 ID）→ 近似高度
_QN_HEIGHT: dict[int, int] = {
    6: 240,
    16: 360,
    32: 480,
    64: 720,
    74: 720,
    80: 1080,
    112: 1080,
    116: 1080,
    120: 2160,
    125: 1440,
    126: 2160,
    127: 4320,
}

#: qn → 展示名（部分接口不带 support_formats 时兜底）
_QN_LABEL: dict[int, str] = {
    6: "240P",
    16: "360P",
    32: "480P",
    64: "720P",
    74: "720P60",
    80: "1080P",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
    125: "2K",
    126: "杜比视界",
    127: "8K",
}

#: DASH 编码（codecid）优先级：数字越小越优先。AVC 兼容性最好，其次 HEVC、AV1。
#: B站同一个 qn 会同时返回 7/12/13 三条流，必须只保留一条（否则前端出现同名清晰度）。
_CODEC_PRIORITY: dict[int, int] = {7: 0, 12: 1, 13: 2}
#: 未知编码的优先级（排在已知编码之后）
_CODEC_PRIORITY_OTHER = 3

_API_VIEW = "https://api.bilibili.com/x/web-interface/view"
_API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

#: 分P 页 URL 参数
_PAGE_RE: re.Pattern[str] = re.compile(r"[?&]p=(\d+)")


def _height_for_qn(qn: int) -> int:
    """清晰度 ID → 近似高度。

    Args:
        qn: B站清晰度 ID。

    Returns:
        高度像素。
    """
    return _QN_HEIGHT.get(qn, 720)


def _fps_for_qn(qn: int) -> int:
    """清晰度 ID → 帧率。

    Args:
        qn: B站清晰度 ID。

    Returns:
        帧率（60 或 30）。
    """
    return 60 if qn in {74, 116} else 30


def _codec_priority(codecid: int) -> int:
    """DASH 编码优先级（数字越小越优先）。

    Args:
        codecid: 编码 ID（7=AVC / 12=HEVC / 13=AV1，其它视为最低优先）。

    Returns:
        优先级值。
    """
    return _CODEC_PRIORITY.get(codecid, _CODEC_PRIORITY_OTHER)


def _qn_for_dash_entry(entry: dict[str, Any], height: int | None) -> int:
    """确定一条 DASH 视频流对应的 qn（清晰度 ID）。

    B站 DASH 的 ``id`` 字段本身就是 qn，必须优先采用；只有 ``id`` 缺失时才用高度
    反查 ``_QN_HEIGHT``。老视频的 DASH 高度可能是 384 这类非标准值（不在表里），
    反查会失败——此时返回 0，由调用方按 ``dash-h<height>-<codecid>`` 生成 id，
    绝不退回「把 id 当 format_id」的老做法（那会和 support_formats 补出的占位项撞车）。

    Args:
        entry: DASH 视频流条目。
        height: 已解析的高度（可能为 None）。

    Returns:
        qn；无法确定时返回 0。
    """
    qn = as_int(entry.get("id")) or 0
    if qn:
        return qn
    if height:
        return next((key for key, value in _QN_HEIGHT.items() if value == height), 0)
    return 0


def _dash_rank(candidate: dict[str, Any]) -> tuple[int, int]:
    """同一 qn 多条编码流之间的择优排序键（越小越好）。

    Args:
        candidate: 归一化后的候选流（含 ``codecid`` / ``bandwidth``）。

    Returns:
        ``(编码优先级, -码率)``。
    """
    return (
        _codec_priority(int(candidate.get("codecid") or 0)),
        -int(candidate.get("bandwidth") or 0),
    )


def _bvid_from_url(url: str) -> str | None:
    """从 URL 中提取 BV 号。

    Args:
        url: 作品链接。

    Returns:
        BV 号；没有返回 None。
    """
    match = _BVID_RE.search(url or "")
    return match.group(1) if match else None


def _aid_from_url(url: str) -> int | None:
    """从 URL 中提取 av 号。

    Args:
        url: 作品链接。

    Returns:
        av 号；没有返回 None。
    """
    match = _AID_RE.search(url or "")
    return int(match.group(1)) if match else None


def _page_from_url(url: str) -> int:
    """从 URL 的 ``?p=N`` 参数提取分P序号。

    Args:
        url: 作品链接。

    Returns:
        分P序号（从 1 开始，缺省 1）。
    """
    match = _PAGE_RE.search(url or "")
    if not match:
        return 1
    return max(1, int(match.group(1)))


def _unwrap(raw: Any) -> Any:
    """剥掉 ``{"code":0,"data":...}`` 外层。

    Args:
        raw: 原始结构。

    Returns:
        内层数据。
    """
    payload: Any = raw
    for _ in range(6):
        if not isinstance(payload, dict):
            break
        if "bvid" in payload or "dash" in payload or "durl" in payload or "owner" in payload:
            break
        if "data" not in payload:
            break
        payload = payload.get("data")
    return payload


def _extract_view_play(payload: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """从夹具结构中分离 view 与 playurl。

    Args:
        payload: 已解析结构。

    Returns:
        ``(view, playurl)``；两者都缺失时返回 ``(None, None)``。
    """
    if not isinstance(payload, dict):
        return None, None

    if "view" in payload:
        view = _unwrap(payload.get("view"))
        play = _unwrap(payload.get("playurl")) if payload.get("playurl") is not None else None
        return (
            view if isinstance(view, dict) else None,
            play if isinstance(play, dict) else None,
        )

    has_view = any(key in payload for key in ("bvid", "owner", "pages", "stat"))
    has_play = any(key in payload for key in ("dash", "durl", "accept_quality", "support_formats"))
    if has_view and not has_play:
        return payload, None
    if has_play and not has_view:
        return None, payload
    if has_view and has_play:
        return payload, payload

    view = find_dict_with(payload, "bvid") or find_dict_with(payload, "title", "owner")
    play = find_dict_with(payload, "dash") or find_dict_with(payload, "durl")
    return view, play


def _support_flags(play: dict[str, Any]) -> dict[int, dict[str, bool]]:
    """从 support_formats 提取每个 qn 的登录/会员标记（兼容夹具直给字段）。

    Args:
        play: playurl 数据。

    Returns:
        ``qn -> {"need_login": bool, "need_vip": bool}``。
    """
    flags: dict[int, dict[str, bool]] = {}
    entries = play.get("support_formats")
    if not isinstance(entries, list):
        return flags
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        qn = as_int(entry.get("quality"))
        if not qn:
            continue
        flags[qn] = {
            "need_login": bool(entry.get("need_login") or entry.get("needLogin")),
            "need_vip": bool(
                entry.get("need_vip") or entry.get("vip_only") or entry.get("needVip")
            ),
        }
    return flags


def _dash_audio_url(play: dict[str, Any]) -> str | None:
    """取 DASH 音频流地址（选码率最高的一条）。

    Args:
        play: playurl 数据。

    Returns:
        音频地址；无 DASH 音频返回 None。
    """
    dash = play.get("dash")
    if not isinstance(dash, dict):
        return None
    audios = dash.get("audio")
    if not isinstance(audios, list):
        return None
    best: dict[str, Any] | None = None
    best_bandwidth = -1
    for entry in audios:
        if not isinstance(entry, dict):
            continue
        bandwidth = as_int(entry.get("bandwidth")) or 0
        url = first_url(entry.get("baseUrl") or entry.get("base_url"))
        if url and bandwidth > best_bandwidth:
            best, best_bandwidth = entry, bandwidth
    if best is None:
        return None
    return first_url(best.get("baseUrl") or best.get("base_url")) or first_url(best.get("backupUrl"))


def _build_formats(
    view: dict[str, Any],
    play: dict[str, Any] | None,
    *,
    logged_in: bool,
) -> list[VideoFormat]:
    """把 view/playurl 映射为统一清晰度列表。

    Args:
        view: 稿件数据。
        play: playurl 数据（可能为 None）。
        logged_in: 当前是否携带了 Cookie（影响「需登录」标记）。

    Returns:
        清晰度列表（至少一项，便于前端渲染下拉框）。
    """
    formats: list[VideoFormat] = []
    duration = as_int(view.get("duration")) or 0
    play = play or {}
    flags = _support_flags(play)
    audio_url = _dash_audio_url(play)
    #: 已被真实流覆盖的高度 / qn（占位项据此去重，避免同一清晰度出现两次）
    covered_heights: set[int] = set()
    covered_qns: set[int] = set()

    dash = play.get("dash") if isinstance(play.get("dash"), dict) else {}
    videos = dash.get("video") if isinstance(dash.get("video"), list) else []
    # 同一 qn 的多种编码（AVC/HEVC/AV1）只保留一条：优先 AVC，同优先级取码率最大者。
    # 键为 ``(qn, 0)``；qn 未知时退化为 ``(0, height)``，避免不同高度被误合并。
    best_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for entry in videos:
        if not isinstance(entry, dict):
            continue
        url = first_url(entry.get("baseUrl") or entry.get("base_url"))
        if not url:
            url = first_url(entry.get("backupUrl") or entry.get("backup_url"))
        if not url:
            continue
        height = as_int(entry.get("height"))
        qn = _qn_for_dash_entry(entry, height)
        candidate = {
            "qn": qn,
            "codecid": as_int(entry.get("codecid")) or 0,
            "bandwidth": as_int(entry.get("bandwidth")) or 0,
            "height": height,
            "url": url,
            "entry": entry,
        }
        key = (qn, 0) if qn else (0, height or 0)
        current = best_by_key.get(key)
        if current is None or _dash_rank(candidate) < _dash_rank(current):
            best_by_key[key] = candidate

    for candidate in best_by_key.values():
        entry = candidate["entry"]
        qn = int(candidate["qn"])
        codecid = int(candidate["codecid"])
        url = str(candidate["url"])
        height = candidate["height"]
        width = as_int(entry.get("width"))
        fps = as_int(entry.get("frameRate") or entry.get("frame_rate")) or 30
        bitrate = as_int(entry.get("bandwidth"))
        height_value = height or (_height_for_qn(qn) if qn else 0)
        covered_heights.add(height_value)
        if qn:
            covered_qns.add(qn)
        flag = flags.get(qn, {})
        need_vip = bool(flag.get("need_vip")) or height_value >= 1440
        # format_id 必须全局唯一且跨两次解析稳定（下载任务会重新解析后按 id 回查）：
        # 带上 qn 与 codecid，既不与占位项的 ``str(qn)`` 撞车，也不与其它编码撞车。
        format_id = f"dash-{qn}-{codecid}" if qn else f"dash-h{height_value}-{codecid}"
        formats.append(
            VideoFormat(
                format_id=format_id,
                quality=normalize_quality(height_value, fps),
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                container="mp4",
                size_bytes=int(bitrate * duration / 8) if bitrate and duration else None,
                need_login=False,
                need_vip=need_vip,
                note="会员专属清晰度，仅展示" if need_vip else None,
                url=url,
                audio_url=audio_url,
            )
        )

    if not formats:
        # 渐进式 mp4（durl）：音视频已封装在一起
        dimension = view.get("dimension") if isinstance(view.get("dimension"), dict) else {}
        durl = play.get("durl") if isinstance(play.get("durl"), list) else []
        for entry in durl:
            if not isinstance(entry, dict):
                continue
            url = first_url(entry.get("url"))
            if not url:
                continue
            size = as_int(entry.get("size"))
            quality_qn = 0
            accept = play.get("accept_quality")
            if isinstance(accept, list) and accept:
                quality_qn = as_int(accept[0]) or 0
            height = _height_for_qn(quality_qn) if quality_qn else as_int(dimension.get("height"))
            formats.append(
                VideoFormat(
                    format_id=f"durl-{quality_qn}",
                    quality=normalize_quality(height, _fps_for_qn(quality_qn)),
                    width=as_int(dimension.get("width")),
                    height=height,
                    fps=_fps_for_qn(quality_qn) if quality_qn else 30,
                    bitrate=int(size * 8 / duration) if size and duration else None,
                    container="mp4",
                    size_bytes=size,
                    url=url,
                )
            )
            # 渐进式流已覆盖该 qn：不再为它补占位项（否则同一清晰度出现两次）
            if quality_qn:
                covered_qns.add(quality_qn)
            if height:
                covered_heights.add(height)
            break

    # 缺失清晰度：按 support_formats / accept_quality 补占位项（无媒体地址）
    qns = sorted(flags) if flags else [
        int(as_int(qn) or 0) for qn in (play.get("accept_quality") or []) if as_int(qn)
    ]
    if not qns and not formats:
        # playurl 完全缺失（例如只拿到 view）：按“未登录匿名可用”的常见档位兜底
        qns = [116, 80, 64, 32, 16]
    for qn in qns:
        height = _height_for_qn(qn)
        if qn in covered_qns or height in covered_heights:
            continue
        need_vip = height >= 1440 or bool(flags.get(qn, {}).get("need_vip"))
        # 占位项没有媒体地址：未登录时平台不会给地址，必须让前端置灰、后端不默认选中；
        # 会员专属档位优先报「需会员」，不同时标 need_login（否则会误报登录）。
        need_login = (not need_vip) and not logged_in
        if need_login:
            note = "需登录后可选"
        elif need_vip:
            note = "会员专属清晰度，仅展示"
        else:
            note = "平台未返回该清晰度地址"
        formats.append(
            VideoFormat(
                format_id=str(qn),
                quality=normalize_quality(height, _fps_for_qn(qn)),
                height=height,
                fps=_fps_for_qn(qn),
                container="mp4",
                need_login=need_login,
                need_vip=need_vip,
                note=note,
                url=None,
            )
        )

    if not formats:
        # 极端兜底：保证 formats 非空（契约 §7.4），下载阶段会重新解析
        formats.append(
            VideoFormat(
                format_id="unknown",
                quality="720P",
                height=720,
                fps=30,
                container="mp4",
                note="平台未返回清晰度信息",
            )
        )

    formats.sort(key=lambda fmt: (fmt.height or 0, fmt.fps or 0), reverse=True)
    return formats


def _warnings_for(formats: list[VideoFormat]) -> list[str]:
    """根据占位清晰度推导降级提示。

    Args:
        formats: 清晰度列表。

    Returns:
        去重后的提示码列表。
    """
    warnings: list[str] = []
    if any(fmt.need_login and not fmt.url for fmt in formats):
        warnings.append("LOGIN_REQUIRED")
    if formats and all(fmt.need_vip or not fmt.url for fmt in formats) and any(
        fmt.need_vip for fmt in formats
    ):
        warnings.append("VIP_REQUIRED")
    return warnings


def _parse_view(view: dict[str, Any], play: dict[str, Any] | None, url: str, *, logged_in: bool) -> VideoItem:
    """把 view（+playurl）映射为 VideoItem。

    Args:
        view: 稿件数据。
        play: playurl 数据。
        url: 请求 URL（提取分P）。
        logged_in: 是否携带 Cookie。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 缺少 BVID 或标题等关键字段。
    """
    bvid = as_text(view.get("bvid"), 32) or (_bvid_from_url(url) or "")
    aid = as_int(view.get("aid")) or _aid_from_url(url)
    if not bvid and not aid:
        raise VideoParseError("未能识别 B站立项 ID（页面结构可能已调整）")
    item_id = bvid or f"av{aid}"
    page_no = _page_from_url(url)
    pages = view.get("pages") if isinstance(view.get("pages"), list) else []
    page_info = pages[page_no - 1] if 0 < page_no <= len(pages) else {}
    if isinstance(page_info, dict) and as_int(page_info.get("cid")):
        view = {**view, "cid": page_info.get("cid")}

    title = as_text(view.get("title"), 200) or "未命名作品"
    owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
    stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
    stats = (
        {
            "like": as_int(stat.get("like")) or 0,
            "comment": as_int(stat.get("reply")) or 0,
            "share": as_int(stat.get("share")) or 0,
            "play": as_int(stat.get("view")) or 0,
        }
        if stat
        else None
    )
    duration = as_int(page_info.get("duration")) or as_int(view.get("duration")) or 0
    formats = _build_formats(view, play, logged_in=logged_in)
    cover = as_text(view.get("pic"), 1000) or None
    if cover and cover.startswith("http://"):
        cover = "https://" + cover[7:]
    canonical = f"https://www.bilibili.com/video/{item_id}"
    if page_no > 1:
        canonical = f"{canonical}?p={page_no}"
    return VideoItem(
        platform="bilibili",
        item_id=item_id,
        url=canonical,
        media_type="video",
        title=title,
        description=as_text(view.get("desc"), 2000),
        author=Author(
            name=as_text(owner.get("name"), 100) or "未知作者",
            id=as_text(owner.get("mid"), 64) or None,
            avatar_url=as_text(owner.get("face"), 1000) or None,
        ),
        cover_url=cover,
        duration_sec=duration,
        published_at=to_iso(as_int(view.get("pubdate"))),
        stats=stats,
        formats=formats,
        images=[],
        piece_count=max(1, len(pages) or as_int(view.get("videos")) or 1),
        warnings=_warnings_for(formats),
        parse_source="api",
    )


def _raise_api_error(code: int, message: Any = None) -> None:
    """按 B站业务错误码抛出对应的契约异常。

    Args:
        code: 接口返回的 ``code``。
        message: 接口返回的 ``message``（仅用于排障，不直接透传给用户）。

    Raises:
        ItemNotFoundError: 稿件不存在/已删除/不可见。
        RegionLockedError: 地区或权限限制。
        LoginRequiredError: 需要登录态。
        VideoParseError: 其它业务错误。
    """
    if code in {-404, 62002, 62004}:
        raise ItemNotFoundError("作品不存在或已被作者删除/设为私密")
    if code in {-403, -10403, 62012}:
        raise RegionLockedError("该作品在当前地区不可用或仅限特定用户观看")
    if code in {-101, -111}:
        raise LoginRequiredError("该作品需要登录后才能查看，请先在设置中填写 B站 Cookie")
    detail = as_text(message, 100)
    raise VideoParseError(f"B站接口返回错误（code={code}）{('：' + detail) if detail else ''}")


def parse_fixture(
    raw: str | bytes | dict[str, Any],
    url: str,
    *,
    logged_in: bool = False,
) -> VideoItem:
    """离线解析 B站 view / playurl 原始内容（不触网，供夹具测试使用）。

    Args:
        raw: JSON 文本、字节串，或 ``{"view": ..., "playurl": ...}`` 组合字典。
        url: 稿件 URL（提取 BV 号与分P）。
        logged_in: 是否按「已登录」处理（影响 need_login 标记）。

    Returns:
        VideoItem。

    Raises:
        VideoParseError: 无法解析。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VideoParseError("B站返回的数据不是合法 JSON") from exc

    if isinstance(payload, dict) and "view" not in payload and "code" in payload:
        code = as_signed_int(payload.get("code"))
        if code != 0:
            _raise_api_error(code, payload.get("message"))

    view, play = _extract_view_play(_unwrap(payload))
    if view is None:
        raise VideoParseError("B站数据中未找到稿件信息")
    if play is None:
        play = {}

    item = _parse_view(view, play, url, logged_in=logged_in)
    # 分P 的 cid 必须来自 pages，playurl 才对应正确的分P
    return item


class BilibiliAdapter:
    """B站平台适配器。"""

    name = "bilibili"
    hosts = tuple(registry.PLATFORMS["bilibili"])

    def match(self, url: str) -> bool:
        """判断 URL 是否属于 B站。

        Args:
            url: 目标 URL。

        Returns:
            是否匹配。
        """
        return registry.platform_for_url(url) == self.name

    def normalize(self, url: str) -> str:
        """展开 b23.tv 短链。

        Args:
            url: 原始 URL。

        Returns:
            长链或原 URL。
        """
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host == "b23.tv":
            return registry.expand_shortlink(url)
        return url

    def _headers(self, ctx: ParseContext) -> dict[str, str]:
        """构造 B站 API 请求头。

        Args:
            ctx: 解析上下文。

        Returns:
            请求头字典（含 Cookie 时注入，绝不写入日志）。
        """
        headers = {
            "User-Agent": net.UA_DESKTOP,
            "Referer": "https://www.bilibili.com/",
        }
        if ctx.cookie:
            headers["Cookie"] = ctx.cookie
        return headers

    def _api(self, url: str, ctx: ParseContext) -> dict[str, Any]:
        """请求 B站 JSON 接口。

        Args:
            url: 接口地址。
            ctx: 解析上下文。

        Returns:
            响应 JSON 字典。

        Raises:
            VideoParseError: 网络失败或响应非法。
        """
        try:
            _final, data, status = net.http_get_json(
                url, timeout=ctx.timeout, headers=self._headers(ctx)
            )
        except net.NetError as exc:
            raise VideoParseError("B站接口请求失败，请检查网络后重试") from exc
        if status >= 500:
            raise VideoParseError("B站接口暂时不可用，请稍后再试")
        if not isinstance(data, dict):
            raise VideoParseError("B站接口返回了无法识别的数据")
        return data

    def _fetch_view(self, url: str, bvid: str | None, aid: int | None, ctx: ParseContext) -> dict[str, Any]:
        """请求稿件详情接口并处理业务错误码。

        Args:
            url: 原始 URL。
            bvid: BV 号。
            aid: av 号。
            ctx: 解析上下文。

        Returns:
            稿件数据。

        Raises:
            ItemNotFoundError / RegionLockedError / VideoParseError: 视业务错误码而定。
        """
        query = f"bvid={bvid}" if bvid else f"aid={aid}"
        data = self._api(f"{_API_VIEW}?{query}", ctx)
        code = as_signed_int(data.get("code"))
        if code == 0:
            view = data.get("data")
            if not isinstance(view, dict):
                raise VideoParseError("B站稿件接口返回空数据")
            return view
        _raise_api_error(code, data.get("message"))

    def _fetch_playurl(
        self,
        view: dict[str, Any],
        url: str,
        ctx: ParseContext,
    ) -> dict[str, Any] | None:
        """请求 playurl 接口（DASH 优先，渐进式兜底）。

        Args:
            view: 稿件数据。
            url: 原始 URL。
            ctx: 解析上下文。

        Returns:
            playurl 数据；完全失败返回 None（上层用 view 兜底）。

        Raises:
            CookieInvalidError: 已配置 Cookie 但被判定失效。
        """
        bvid = as_text(view.get("bvid"), 32)
        aid = as_int(view.get("aid"))
        page_no = _page_from_url(url)
        pages = view.get("pages") if isinstance(view.get("pages"), list) else []
        cid = as_int(view.get("cid")) or 0
        if not cid and pages:
            index = min(max(page_no - 1, 0), len(pages) - 1)
            page_info = pages[index]
            if isinstance(page_info, dict):
                cid = as_int(page_info.get("cid")) or 0
        if not cid:
            return None

        ident = f"bvid={bvid}" if bvid else f"avid={aid}"
        for fnval, qn in ((4048, 116), (1, 64)):
            query = f"{ident}&cid={cid}&qn={qn}&fnval={fnval}&fourk=1&platform=pc"
            try:
                data = self._api(f"{_API_PLAYURL}?{query}", ctx)
            except VideoParseError:
                continue
            code = as_signed_int(data.get("code"))
            if code == 0 and isinstance(data.get("data"), dict):
                return data["data"]
            if code in {-101, -111, -10403}:
                if ctx.cookie:
                    raise CookieInvalidError("登录信息已失效，请重新填写 Cookie")
                return None
        return None

    def parse(self, url: str, ctx: ParseContext) -> VideoItem:
        """抓取并解析 B站稿件。

        Args:
            url: 稿件链接（可含 b23.tv 短链）。
            ctx: 解析上下文。

        Returns:
            VideoItem。

        Raises:
            ItemNotFoundError: 作品不存在。
            RegionLockedError / CookieInvalidError: 平台侧限制。
            VideoParseError: 解析失败且兜底链耗尽。
        """
        normalized = self.normalize(url)
        bvid = _bvid_from_url(normalized)
        aid = _aid_from_url(normalized)
        if not bvid and not aid:
            raise VideoParseError("链接中未识别到 BV 号/av 号")
        view = self._fetch_view(normalized, bvid, aid, ctx)
        play = self._fetch_playurl(view, normalized, ctx)
        item = _parse_view(view, play, normalized, logged_in=bool(ctx.cookie))
        return item


#: 模块级单例（registry.get_adapter 依赖该属性名）
ADAPTER = BilibiliAdapter()


__all__ = ["ADAPTER", "BilibiliAdapter", "parse_fixture"]
