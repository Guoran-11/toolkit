"""yt-dlp 兜底解析（可选引擎）。

契约 §4.1 的降级链最后一环：平台接口/页面都解析失败时，尝试调用外部 ``yt-dlp``
进程。它是**可选依赖**——未安装时 :func:`available` 返回 False，调用方静默跳过，
绝不影响主链路。

实现约束：
    · 只调用本地可执行文件，不做网络以外的任何集成（AGPL 合规：外部进程调用）；
    · 子进程 20 秒超时，输出只取 stdout 的 JSON；
    · 任何异常都静默返回 None —— 兜底失败不应把更具体的平台错误码覆盖掉。
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from app.core.engines import ytdlp_path
from app.modules.video.adapters.base import ParseContext, as_int, as_text, normalize_quality, to_iso
from app.modules.video.models import Author, VideoFormat, VideoItem

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 20


def _no_window() -> dict[str, int]:
    """返回 Windows 下抑制控制台窗口的 subprocess 参数。

    Returns:
        ``{"creationflags": CREATE_NO_WINDOW}``；非 Windows 返回空字典。
    """
    import os

    if os.name != "nt":
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}


def available() -> bool:
    """yt-dlp 是否可用。

    Returns:
        可执行文件存在时返回 True。
    """
    try:
        return ytdlp_path() is not None
    except Exception:  # noqa: BLE001 - 引擎探测异常不应影响主链路
        return False


def parse(url: str, platform: str, ctx: ParseContext) -> VideoItem | None:
    """用 yt-dlp 解析作品（失败返回 None）。

    Args:
        url: 作品链接。
        platform: 平台标识（写入结果）。
        ctx: 解析上下文（仅使用 cookie 与超时）。

    Returns:
        VideoItem；yt-dlp 不可用或解析失败返回 None。
    """
    exe = None
    try:
        exe = ytdlp_path()
    except Exception:  # noqa: BLE001
        exe = None
    if exe is None:
        return None

    args = [str(exe), "--dump-single-json", "--no-warnings", "--no-playlist", url]
    if ctx.cookie:
        args.extend(["--add-header", f"Cookie:{ctx.cookie}"])

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=_TIMEOUT_SEC,
            check=False,
            **_no_window(),  # type: ignore[arg-type]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("yt-dlp 调用失败（已跳过兜底）：%s", exc)
        return None

    if completed.returncode != 0 or not completed.stdout:
        logger.debug("yt-dlp 解析失败（返回码 %s），已跳过兜底", completed.returncode)
        return None
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _item_from_payload(payload, platform)


def _item_from_payload(payload: dict[str, Any], platform: str) -> VideoItem | None:
    """把 yt-dlp 的 ``--dump-single-json`` 结果映射为 VideoItem。

    Args:
        payload: yt-dlp JSON。
        platform: 平台标识。

    Returns:
        VideoItem；关键字段缺失返回 None。
    """
    item_id = as_text(payload.get("id"), 128)
    if not item_id:
        return None
    duration = as_int(payload.get("duration")) or 0
    audio_url = _best_audio_url(payload)
    formats: list[VideoFormat] = []
    seen: set[str] = set()
    raw_formats = payload.get("formats") if isinstance(payload.get("formats"), list) else []
    for index, entry in enumerate(raw_formats):
        if not isinstance(entry, dict):
            continue
        media_url = as_text(entry.get("url"), 4000)
        if not media_url or media_url in seen:
            continue
        vcodec = as_text(entry.get("vcodec"), 40)
        if vcodec == "none":
            continue  # 纯音频轨单独处理
        seen.add(media_url)
        height = as_int(entry.get("height"))
        width = as_int(entry.get("width"))
        fps = as_int(entry.get("fps"))
        bitrate = as_int(entry.get("tbr"))
        size = as_int(entry.get("filesize")) or as_int(entry.get("filesize_approx"))
        formats.append(
            VideoFormat(
                format_id=as_text(entry.get("format_id"), 64) or f"fmt-{index}",
                quality=normalize_quality(height, fps),
                width=width,
                height=height,
                fps=fps,
                bitrate=int(bitrate * 1000) if bitrate else None,
                container=as_text(entry.get("ext"), 16) or "mp4",
                size_bytes=size,
                note="yt-dlp 兜底解析",
                url=media_url,
                audio_url=audio_url,
            )
        )

    if not formats:
        return None

    formats.sort(key=lambda fmt: fmt.height or 0, reverse=True)
    cover = as_text(payload.get("thumbnail"), 1000) or None
    return VideoItem(
        platform=platform,
        item_id=item_id,
        url=as_text(payload.get("webpage_url"), 1000) or "",
        media_type="video",
        title=as_text(payload.get("title"), 200) or "未命名作品",
        description=as_text(payload.get("description"), 2000),
        author=Author(
            name=as_text(payload.get("uploader") or payload.get("channel"), 100) or "未知作者",
            id=as_text(payload.get("uploader_id") or payload.get("channel_id"), 128) or None,
        ),
        cover_url=cover,
        duration_sec=duration,
        published_at=to_iso(as_int(payload.get("timestamp"))),
        formats=formats,
        parse_source="ytdlp",
    )


def _best_audio_url(payload: dict[str, Any]) -> str | None:
    """从 yt-dlp 格式列表里选码率最高的纯音频地址。

    Args:
        payload: yt-dlp JSON。

    Returns:
        音频地址；没有独立音频轨返回 None。
    """
    raw_formats = payload.get("formats") if isinstance(payload.get("formats"), list) else []
    best: tuple[int, str] | None = None
    for entry in raw_formats:
        if not isinstance(entry, dict):
            continue
        if as_text(entry.get("vcodec"), 40) != "none":
            continue
        media_url = as_text(entry.get("url"), 4000)
        if not media_url:
            continue
        bitrate = as_int(entry.get("abr")) or as_int(entry.get("tbr")) or 0
        if best is None or bitrate > best[0]:
            best = (bitrate, media_url)
    return best[1] if best else None


__all__ = ["available", "parse"]
