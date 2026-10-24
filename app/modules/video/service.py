"""视频模块编排层：解析、下载任务、历史/去重、设置、限流/熔断与审计。

分层定位（契约 §4）：router 只做协议转换；本模块负责错误码映射与任务编排；
适配器只做「链接 → 元数据」，下载器只做「媒体地址 → 产物」。

几个关键取舍：
    1. **解析逐条容错**：单条失败不影响整批（与音频探测踩过的坑一致）；但若整批
       一条都不支持，则直接抛 ``PLATFORM_UNSUPPORTED``，避免前端拿到一个「全是错误
       卡片」的 200 响应。
    2. **任务提交前预检**：磁盘、限流、熔断、平台开关都在提交前完成，避免创建一堆
       注定失败的任务占满队列。
    3. **历史回写**：任务成功时的 ``file_id`` 由任务管理器在 job 返回后才登记，
       因此用轻量守护线程轮询终态再写历史（不修改核心层，也不阻塞请求）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Any

from app.api.deps import ConfigDep
from app.core.config import get_settings
from app.core.engines import engine_probe
from app.core.errors import (
    DiskFullError,
    LinkUnrecognizedError,
    NotFoundError,
    PlatformUnsupportedError,
    RiskControlError,
    ServiceDisabledError,
    ToolkitError,
    ValidationError,
    VideoParseError,
)
from app.core.security import write_audit_log
from app.core.storage import disk_free_bytes, registry
from app.core.tasks import (
    TERMINAL_STATUSES,
    JobContext,
    JobFunc,
    JobResult,
    TaskInput,
    get_task_manager,
)
from app.modules.video import credentials, history, limiter
from app.modules.video.adapters import registry as adapters
from app.modules.video.adapters.base import ParseContext, pick_default_format
from app.modules.video.cache import parse_cache
from app.modules.video.downloader import make_download_job
from app.modules.video.models import VideoItem
from app.modules.video.net import NetError

logger = logging.getLogger(__name__)

_DOUYIN_ID_RE: re.Pattern[str] = re.compile(r"(?:video|note|slides)/(\d{6,})|[?&]modal_id=(\d{6,})")
_BILIBILI_ID_RE: re.Pattern[str] = re.compile(r"(BV[0-9A-Za-z]{10})")
_KUAISHOU_ID_RE: re.Pattern[str] = re.compile(r"(?:short-video|photo|video)/([A-Za-z0-9_-]{6,})")
_VALID_OUTPUTS = frozenset({"mp4", "mp3", "m4a"})


# ---------------------------------------------------------------- 模块开关


def module_enabled() -> bool:
    """视频模块是否启用。

    Returns:
        ``[video] enabled`` 的当前值（配置热读）。
    """
    return bool(get_settings().video.enabled)


def require_video_enabled(settings: ConfigDep) -> None:
    """FastAPI 依赖：模块关闭时所有 ``/api/video/*`` 返回 503。

    Args:
        settings: 全局配置。

    Raises:
        ServiceDisabledError: 模块未启用。
    """
    if not settings.video.enabled:
        raise ServiceDisabledError("视频下载模块已被管理员关闭")


# ---------------------------------------------------------------- 平台与引擎


def platforms_payload() -> dict[str, Any]:
    """构造 ``GET /api/video/platforms`` 响应。

    Returns:
        契约 §3.1 结构（平台能力矩阵、开关、Cookie 状态、引擎状态）。
    """
    video = get_settings().video
    try:
        engines = engine_probe.probe()
        engine_info = {
            name: {"state": engines[name].state, "version": engines[name].version}
            for name in ("ffmpeg", "ytdlp")
            if name in engines
        }
    except Exception as exc:  # noqa: BLE001 - 引擎探测失败不应让平台列表 500
        logger.warning("视频平台列表的引擎探测失败：%s", exc)
        engine_info = {
            "ffmpeg": {"state": "unknown", "version": None},
            "ytdlp": {"state": "unknown", "version": None},
        }
    return {
        "enabled": bool(video.enabled),
        "items": adapters.iter_platforms(),
        "max_urls": int(video.max_urls),
        "prefer_quality": video.prefer_quality,
        "prefer_format": video.prefer_format,
        "engine": engine_info,
    }


# ---------------------------------------------------------------- 解析


def _item_id_hint(url: str, platform: str) -> str | None:
    """从 URL 中尽量提取作品 ID（用于缓存键与去重）。

    Args:
        url: 作品链接。
        platform: 平台标识。

    Returns:
        作品 ID；无法识别返回 None。
    """
    if platform == "douyin":
        match = _DOUYIN_ID_RE.search(url)
        if match:
            return match.group(1) or match.group(2)
    elif platform == "bilibili":
        match = _BILIBILI_ID_RE.search(url)
        if match:
            return match.group(1)
    elif platform == "kuaishou":
        match = _KUAISHOU_ID_RE.search(url)
        if match:
            return match.group(1)
    return None


def _cache_lookup(platform: str, url: str, item_id: str | None = None) -> VideoItem | None:
    """按 URL / 作品 ID 查解析缓存。

    Args:
        platform: 平台标识。
        url: 作品链接。
        item_id: 作品 ID（可空）。

    Returns:
        缓存的 VideoItem；未命中返回 None。
    """
    for key in _cache_keys(platform, url, item_id):
        cached = parse_cache.get(key)
        if isinstance(cached, VideoItem):
            return cached
    return None


def _cache_keys(platform: str, url: str, item_id: str | None = None) -> list[str]:
    """构造缓存键候选（URL 形态与作品 ID 形态）。

    Args:
        platform: 平台标识。
        url: 作品链接。
        item_id: 作品 ID（可空）。

    Returns:
        缓存键列表。
    """
    keys = [f"url:{url.strip()}"]
    resolved_id = item_id or _item_id_hint(url, platform)
    if resolved_id:
        keys.insert(0, f"{platform}:{resolved_id}")
    return keys


def _store_cache(platform: str, url: str, item: VideoItem) -> None:
    """把解析结果写入缓存（URL 与作品 ID 两个键）。

    Args:
        platform: 平台标识。
        url: 原始链接。
        item: 解析结果。
    """
    parse_cache.put(f"{platform}:{item.item_id}", item)
    parse_cache.put(f"url:{url.strip()}", item)


def _login_detail(item: VideoItem) -> str:
    """构造「需登录」的降级文案（尽量告知当前可下载的最高清晰度）。

    Args:
        item: 作品元数据。

    Returns:
        用户可读文案。
    """
    playable = [fmt for fmt in item.formats if fmt.url and not fmt.need_vip]
    if playable:
        best = max(playable, key=lambda fmt: (fmt.height or 0, fmt.fps or 0))
        return f"该清晰度需要登录，当前仅可下载 {best.quality}"
    return "该清晰度需要登录，请先在设置中填写对应平台的 Cookie"


def _parse_one(
    url: str,
    platform: str | None,
    quality: str,
) -> dict[str, Any]:
    """解析单条链接（失败返回结构化错误，不抛异常）。

    Args:
        url: 链接。
        platform: 平台标识；None 表示不支持。
        quality: 默认清晰度偏好。

    Returns:
        单条结果字典（契约 §3.2 的 ``results[]`` 元素）。
    """
    if platform is None:
        return {
            "ok": False,
            "url": url,
            "cached": False,
            "error": {
                "code": "PLATFORM_UNSUPPORTED",
                "detail": "暂不支持该平台，当前支持抖音、B站、快手",
            },
        }
    if not get_settings().video.platform_enabled(platform):
        return {
            "ok": False,
            "url": url,
            "cached": False,
            "error": {"code": "PLATFORM_UNSUPPORTED", "detail": "该平台已被管理员关闭"},
        }

    try:
        limiter.assert_platform_available(platform)
    except RiskControlError as exc:
        return {
            "ok": False,
            "url": url,
            "cached": False,
            "error": {
                "code": exc.code,
                "detail": exc.detail,
                "retry_after": exc.extra.get("retry_after"),
            },
        }

    item = _cache_lookup(platform, url)
    cached = item is not None
    if item is None:
        ctx = ParseContext(
            platform=platform,
            cookie=credentials.get_cookie(platform),
            prefer_quality=quality,
            timeout=5.0,
            mobile=platform in {"douyin", "kuaishou"},
        )
        try:
            adapter = adapters.get_adapter(platform)
            item = adapter.parse(url, ctx)
        except ToolkitError as exc:
            if isinstance(exc, (RiskControlError, VideoParseError)):
                limiter.record_platform_failure(platform)
            return {
                "ok": False,
                "url": url,
                "cached": False,
                "error": {"code": exc.code, "detail": exc.detail},
            }
        except NetError as exc:
            limiter.record_platform_failure(platform)
            return {
                "ok": False,
                "url": url,
                "cached": False,
                "error": {"code": "PARSE_FAILED", "detail": f"网络请求失败：{exc}"},
            }
        limiter.record_platform_success(platform)
        _store_cache(platform, url, item)

    payload_item = item.to_api()
    # 后端统一给出「默认应选哪一档」：前端直接用它，避免自己按顺序取第一个
    # （占位项排在真实流前面时会选中无媒体地址的档位，生产下载失败即由此而来）。
    default_format = pick_default_format(item.formats, quality)
    default_format_id = str(default_format.get("format_id") or "") if default_format else ""
    payload_item["default_format_id"] = default_format_id or None
    if "LOGIN_REQUIRED" in item.warnings:
        return {
            "ok": False,
            "url": item.url or url,
            "cached": cached,
            "error": {"code": "LOGIN_REQUIRED", "detail": _login_detail(item)},
            "item": payload_item,
        }
    return {"ok": True, "url": item.url or url, "cached": cached, "item": payload_item}


def parse_urls(
    urls: list[str],
    prefer_quality: str | None = None,
    *,
    ip: str = "",
    user: Any = None,
) -> dict[str, Any]:
    """解析一批链接（契约 §3.2）。

    Args:
        urls: 用户输入（可能是含链接的整段分享文案）。
        prefer_quality: 默认清晰度偏好；None 时读配置。
        ip: 客户端 IP（限流维度）。
        user: 鉴权主体（限流维度）。

    Returns:
        ``{"results": [...], "elapsed_ms": int}``。

    Raises:
        ValidationError: 链接数超过上限。
        LinkUnrecognizedError: 没有识别到任何链接。
        PlatformUnsupportedError: 整批都不受支持（或平台已关闭）。
    """
    video = get_settings().video
    if not video.enabled:
        raise ServiceDisabledError("视频下载模块已被管理员关闭")

    if ip or user is not None:
        limiter.check_parse(ip or "unknown", user)

    text = "\n".join(str(item) for item in (urls or []) if str(item).strip())
    links = adapters.extract_links(text, limit=max(1, int(video.max_urls)) + 1)
    if not links:
        raise LinkUnrecognizedError("没有识别到作品链接，请粘贴完整分享文案或链接")
    if len(links) > int(video.max_urls):
        raise ValidationError(f"单次最多解析 {video.max_urls} 条链接，当前 {len(links)} 条")

    quality = prefer_quality or video.prefer_quality
    started = time.perf_counter()

    classified: list[tuple[str, str | None]] = []
    supported = 0
    for url in links:
        platform = adapters.platform_for_url(url)
        if platform is not None and video.platform_enabled(platform):
            supported += 1
        classified.append((url, platform))

    if supported == 0:
        if classified and all(
            platform is not None and not video.platform_enabled(platform)
            for _url, platform in classified
        ):
            raise PlatformUnsupportedError("该平台已被管理员关闭")
        raise PlatformUnsupportedError("暂不支持该平台，当前支持抖音、B站、快手")

    results = [_parse_one(url, platform, quality) for url, platform in classified]
    return {"results": results, "elapsed_ms": int((time.perf_counter() - started) * 1000)}


# ---------------------------------------------------------------- 下载任务


def _cache_for_download(spec: dict[str, Any]) -> VideoItem | None:
    """取下载规格对应的缓存作品。

    Args:
        spec: 下载规格。

    Returns:
        缓存的 VideoItem；未命中返回 None。
    """
    return _cache_lookup(spec["platform"], spec["url"], spec.get("item_id"))


def _resolve_item_for_download(spec: dict[str, Any]) -> VideoItem:
    """下载任务执行前解析/刷新作品元数据。

    解析缓存里的媒体直链有时效（B站/抖音签名地址通常几十分钟），任务可能排队几分钟；
    为避免拿过期地址下载，这里优先使用缓存，但缓存超过 5 分钟或缺少媒体地址时重新解析。

    Args:
        spec: 下载规格。

    Returns:
        含媒体地址的 VideoItem。

    Raises:
        ToolkitError: 重新解析失败（错误码透传给任务）。
    """
    item = _cache_for_download(spec)
    if item is not None and item.has_playable_video():
        return item

    platform = spec["platform"]
    ctx = ParseContext(
        platform=platform,
        cookie=credentials.get_cookie(platform),
        prefer_quality=get_settings().video.prefer_quality,
        timeout=5.0,
        mobile=platform in {"douyin", "kuaishou"},
    )
    adapter = adapters.get_adapter(platform)
    fresh = adapter.parse(spec["url"], ctx)
    _store_cache(platform, spec["url"], fresh)
    return fresh


def build_download_job(spec: dict[str, Any]) -> JobFunc:
    """构造一个下载任务的 JobFunc。

    Args:
        spec: 下载规格（url/platform/item_id/format_id/output/title/author/quality）。

    Returns:
        任务函数。
    """

    def job(ctx: JobContext) -> JobResult:
        """执行下载任务（必要时先刷新解析）。

        Args:
            ctx: 任务上下文。

        Returns:
            任务产出。
        """
        item = _resolve_item_for_download(spec)
        inner = make_download_job(
            item,
            format_id=spec.get("format_id"),
            output=spec.get("output") or get_settings().video.prefer_format,
        )
        return inner(ctx)

    return job


def _hash_item_id(item_id: str | None) -> str | None:
    """审计日志用：作品 ID 的 SHA256 前 8 位（不记录原 ID）。

    Args:
        item_id: 原始作品 ID。

    Returns:
        8 位十六进制摘要；空值返回 None。
    """
    if not item_id:
        return None
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:8]


def _audit_download(
    *,
    ip: str,
    platform: str,
    item_id: str | None,
    quality: str,
    size: int,
    result: str,
    error_code: str | None = None,
) -> None:
    """写一条下载审计日志（不含完整 URL / Cookie）。

    Args:
        ip: 客户端 IP。
        platform: 平台标识。
        item_id: 作品 ID（内部哈希后写入）。
        quality: 清晰度。
        size: 产物字节数（未知为 0）。
        result: ``submitted`` / ``success`` / ``failed`` / ``reused``。
        error_code: 错误码（成功时为空）。
    """
    write_audit_log(
        {
            "action": "video.download",
            "ip": ip,
            "platform": platform,
            "item_id": _hash_item_id(item_id),
            "quality": quality,
            "size": int(size or 0),
            "result": result,
            "error_code": error_code,
        }
    )


def _watch_and_record(task_id: str, spec: dict[str, Any], ip: str) -> None:
    """守护线程：等待任务终态后写入历史与审计。

    ``file_id`` 由任务管理器在 job 返回后登记，因此历史只能在任务结束后回写。

    Args:
        task_id: 任务标识。
        spec: 下载规格。
        ip: 客户端 IP。
    """
    manager = get_task_manager()
    deadline = time.monotonic() + int(get_settings().video.task_timeout_sec_video) + 60
    while time.monotonic() < deadline:
        try:
            record = manager.get(task_id)
        except ToolkitError:
            return
        if record.status in TERMINAL_STATUSES:
            cached = _cache_for_download(spec)
            quality = spec.get("quality") or (
                cached.formats[0].quality if cached and cached.formats else ""
            )
            if record.status == "success" and record.output:
                history.history_store.record(
                    platform=spec["platform"],
                    item_id=spec.get("item_id") or (cached.item_id if cached else ""),
                    title=spec.get("title") or (cached.title if cached else ""),
                    author=spec.get("author") or (cached.author.name if cached else ""),
                    cover_url=cached.cover_url if cached else None,
                    quality=quality,
                    size_bytes=int(record.output.get("size") or 0),
                    status="success",
                    task_id=task_id,
                    file_id=str(record.output.get("file_id") or ""),
                    url=spec["url"],
                    expire_at=record.expire_at,
                )
                _audit_download(
                    ip=ip,
                    platform=spec["platform"],
                    item_id=spec.get("item_id"),
                    quality=quality,
                    size=int(record.output.get("size") or 0),
                    result="success",
                )
            else:
                history.history_store.record(
                    platform=spec["platform"],
                    item_id=spec.get("item_id") or (cached.item_id if cached else ""),
                    title=spec.get("title") or (cached.title if cached else ""),
                    author=spec.get("author") or (cached.author.name if cached else ""),
                    cover_url=cached.cover_url if cached else None,
                    quality=quality,
                    status="failed",
                    task_id=task_id,
                    url=spec["url"],
                )
                _audit_download(
                    ip=ip,
                    platform=spec["platform"],
                    item_id=spec.get("item_id"),
                    quality=quality,
                    size=0,
                    result=record.status,
                    error_code=record.error_code,
                )
            return
        time.sleep(0.5)


def _normalize_spec(raw: Any, video: Any) -> dict[str, Any]:
    """校验并规格化单个下载项。

    Args:
        raw: 请求里的下载项（字典）。
        video: ``VideoConfig`` 配置对象。

    Returns:
        规格化后的字典。

    Raises:
        ValidationError: 字段非法。
        PlatformUnsupportedError: 平台不支持/已关闭/链接不匹配。
        RiskControlError: 平台熔断中。
    """
    if not isinstance(raw, dict):
        raise ValidationError("下载项格式不合法")
    url = str(raw.get("url") or "").strip()
    if not url:
        raise ValidationError("下载项缺少作品链接")
    platform = str(raw.get("platform") or "").strip().lower()
    if not platform:
        platform = adapters.platform_for_url(url) or ""
    if platform not in adapters.PLATFORMS:
        raise PlatformUnsupportedError("暂不支持该平台，当前支持抖音、B站、快手")
    if adapters.platform_for_url(url) != platform:
        raise PlatformUnsupportedError("作品链接与所选平台不匹配")
    if not video.platform_enabled(platform):
        raise PlatformUnsupportedError("该平台已被管理员关闭")
    limiter.assert_platform_available(platform)

    output = str(raw.get("output") or video.prefer_format or "mp4").lower()
    if output not in _VALID_OUTPUTS:
        raise ValidationError("输出格式只能是 mp4 / mp3 / m4a")

    return {
        "url": url,
        "platform": platform,
        "item_id": str(raw.get("item_id") or "").strip() or _item_id_hint(url, platform),
        "format_id": str(raw.get("format_id") or "").strip() or None,
        "output": output,
        "title": str(raw.get("title") or "").strip() or "未命名作品",
        "author": str(raw.get("author") or "").strip() or "未知作者",
        "quality": str(raw.get("quality") or "").strip(),
    }


def _reuse_completed(spec: dict[str, Any], ip: str = "") -> dict[str, Any] | None:
    """尝试复用 24h 内同作品的已有产物。

    Args:
        spec: 下载规格。
        ip: 客户端 IP（审计用）。

    Returns:
        已登记任务（追加 ``reused=True``）；未命中返回 None。
    """
    if not spec.get("item_id"):
        return None
    video = get_settings().video
    reused = history.history_store.find_reusable(
        spec["platform"], spec["item_id"], window_hours=int(video.dedupe_window_hours)
    )
    if not reused:
        return None
    try:
        output = registry.get(str(reused.get("file_id") or ""))
    except NotFoundError:
        return None

    record = get_task_manager().register_completed(
        module="video",
        operation="download",
        inputs=[
            TaskInput(
                name=spec["title"],
                size=int(output.size),
                sha256="",
                source=spec["url"],
            )
        ],
        output=output,
    )
    data = record.to_api()
    data["reused"] = True
    _audit_download(
        ip=ip,
        platform=spec["platform"],
        item_id=spec.get("item_id"),
        quality=spec.get("quality") or str(reused.get("quality") or ""),
        size=int(output.size),
        result="reused",
    )
    return data


def submit_downloads(items: list[dict[str, Any]], ip: str = "", user: Any = None) -> list[dict[str, Any]]:
    """提交一批下载任务（契约 §3.3）。

    Args:
        items: 下载项列表。
        ip: 客户端 IP（限流与审计）。
        user: 鉴权主体（限流维度）。

    Returns:
        任务数组（每项为 ``TaskRecord.to_api()``；复用命中追加 ``reused=True``）。

    Raises:
        ValidationError: 未选择作品或数量超限。
        DiskFullError: 磁盘空间不足。
        PlatformUnsupportedError: 平台不支持/已关闭/链接不匹配。
        RiskControlError: 平台熔断中。
        RateLimitedError: 超出下载频率限制。
    """
    video = get_settings().video
    if not video.enabled:
        raise ServiceDisabledError("视频下载模块已被管理员关闭")
    if not items:
        raise ValidationError("请至少选择一个作品")
    if len(items) > int(video.max_urls):
        raise ValidationError(f"单次最多下载 {video.max_urls} 个作品，当前 {len(items)} 个")

    free = disk_free_bytes()
    required = int(video.min_free_gb) * 1024**3
    if required and free < required:
        raise DiskFullError(
            f"磁盘可用空间不足（剩余 {free / 1024**3:.1f} GB，要求至少 {video.min_free_gb} GB），"
            "请清理 storage 目录后重试"
        )

    # 第一阶段：全部校验通过后再提交，避免半批任务 + 一个 4xx 的尴尬状态
    specs: list[dict[str, Any]] = []
    for raw in items:
        limiter.check_download(ip or "unknown", user)
        specs.append(_normalize_spec(raw, video))

    # 第二阶段：去重复用 / 提交任务
    manager = get_task_manager()
    results: list[dict[str, Any]] = []
    for spec in specs:
        reused = _reuse_completed(spec, ip)
        if reused is not None:
            results.append(reused)
            continue

        job = build_download_job(spec)
        record = manager.submit(
            module="video",
            operation="download",
            inputs=[
                TaskInput(name=spec["title"], size=0, sha256="", source=spec["url"])
            ],
            job=job,
            timeout_sec=int(video.task_timeout_sec_video),
        )
        watcher = threading.Thread(
            target=_watch_and_record,
            args=(record.task_id, spec, ip),
            name=f"video-history-{record.task_id[:8]}",
            daemon=True,
        )
        watcher.start()
        _audit_download(
            ip=ip,
            platform=spec["platform"],
            item_id=spec.get("item_id"),
            quality=spec.get("quality"),
            size=0,
            result="submitted",
        )
        results.append(record.to_api())

    return results


__all__ = [
    "build_download_job",
    "module_enabled",
    "parse_urls",
    "platforms_payload",
    "require_video_enabled",
    "submit_downloads",
]
