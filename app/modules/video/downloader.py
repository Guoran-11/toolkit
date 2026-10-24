"""下载器：Range 分片、断点续传、FFmpeg 合并 / 转音频、图文 zip。

契约 §4.3 / §4.4 的落地：
    · 先用 HEAD / ``Range: bytes=0-0`` 探测 ``Content-Length`` 与 Range 支持；
    · 分片写 ``seg-0001.part`` + ``meta.json``，重试时跳过完整分片（断点续传）；
    · 每片重试 3 次（0.5/1/2s 退避），任务级重试 2 次（从已完成分片续传）；
    · 进度映射：准备 1–5、连接 5–15、下载 15–90、合并 90–98、登记 98–100；
    · 取消：分片前后检查 ``ctx.raise_if_cancelled()``，``finally`` 删除临时目录；
    · 大小保护：``Content-Length`` 与累计字节双重检查，超限 ``OUTPUT_TOO_LARGE``；
    · 磁盘保护：低于 ``min_free_gb`` 拒绝执行 ``DISK_FULL``；
    · DASH 音视频分离 → FFmpeg ``-c copy`` 合并；缺 FFmpeg → ``ENGINE_UNAVAILABLE``；
    · 图文 → 按 ``01.jpg`` 顺序下载后 ZIP_DEFLATED 打包。

并发：全局同时下载任务数由 :class:`DownloadGate` 限制为 ``[video] max_concurrent``，
超出者排队等待（可被取消），排队期间不占用线程池之外的资源。
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.core.config import TEMP_DIR, get_settings
from app.core.engines import ffmpeg_path, run_process
from app.core.errors import (
    DiskFullError,
    DownloadFailedError,
    EngineUnavailableError,
    LoginRequiredError,
    OutputTooLargeError,
    VideoParseError,
    VipRequiredError,
)
from app.core.storage import disk_free_bytes, stage_output
from app.core.tasks import JobContext, JobFunc, JobResult, TaskCancelled
from app.modules.video import naming, net
from app.modules.video.adapters import registry as adapters
from app.modules.video.adapters.base import pick_default_format
from app.modules.video.models import VideoFormat, VideoItem

logger = logging.getLogger(__name__)

#: 单个分片的重试次数（契约 §4.3）
_SEGMENT_RETRIES = 3
#: 任务级总尝试次数（首次 + 2 次重试）
_TASK_ATTEMPTS = 3
_TASK_BACKOFF_SEC = (1.0, 2.0)
_SEGMENT_BACKOFF_SEC = (0.5, 1.0, 2.0)
_FFMPEG_TIMEOUT_SEC = 300


# ---------------------------------------------------------------- 并发闸门


class DownloadGate:
    """限制同时执行的下载任务数（可被取消的阻塞式信号量）。"""

    def __init__(self) -> None:
        """初始化闸门。"""
        self._condition = threading.Condition()
        self._active = 0

    def acquire(self, ctx: JobContext) -> None:
        """获取一个下载槽位（阻塞直到有空位或任务被取消）。

        Args:
            ctx: 任务上下文（用于响应取消）。

        Raises:
            TaskCancelled: 等待期间任务被取消。
        """
        while True:
            ctx.raise_if_cancelled()
            limit = max(1, int(get_settings().video.max_concurrent))
            with self._condition:
                if self._active < limit:
                    self._active += 1
                    return
                self._condition.wait(timeout=0.25)

    def release(self) -> None:
        """归还槽位。"""
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify()

    def active(self) -> int:
        """当前正在执行的下载任务数。

        Returns:
            活跃任务数量。
        """
        with self._condition:
            return self._active


#: 进程级下载闸门
download_gate = DownloadGate()


# ---------------------------------------------------------------- 进度工具


class _Progress:
    """把「已完成字节 / 总字节」映射到指定进度区间。"""

    def __init__(self, ctx: JobContext, low: int, high: int) -> None:
        """初始化。

        Args:
            ctx: 任务上下文。
            low: 区间起点（百分比）。
            high: 区间终点（百分比）。
        """
        self._ctx = ctx
        self._low = low
        self._high = high
        self._lock = threading.Lock()

    def set_fraction(self, fraction: float) -> None:
        """按比例更新进度。

        Args:
            fraction: 0.0–1.0 的完成比例。
        """
        clamped = max(0.0, min(1.0, float(fraction)))
        value = int(self._low + (self._high - self._low) * clamped)
        with self._lock:
            self._ctx.progress(value)

    def set_percent(self, percent: int) -> None:
        """直接设置区间内的百分比（会被夹取）。

        Args:
            percent: 全局进度百分比。
        """
        clamped = max(self._low, min(self._high, int(percent)))
        with self._lock:
            self._ctx.progress(clamped)


def _sleep_responsive(ctx: JobContext, seconds: float) -> None:
    """可被取消打断的 sleep。

    Args:
        ctx: 任务上下文。
        seconds: 等待秒数。

    Raises:
        TaskCancelled: 等待期间被取消。
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ctx.raise_if_cancelled()
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


# ---------------------------------------------------------------- 工作目录


def workdir_for(task_id: str) -> Path:
    """任务临时目录（``storage/temp/video-<task_id>``）。

    刻意不用 ``tempfile.mkdtemp``：其 0o700 权限在部分受限环境下会让后续写入失败，
    且目录名需要可预测，才能在服务重启后按任务 ID 续传（契约 §4.4）。

    Args:
        task_id: 任务标识。

    Returns:
        已创建（或已存在）的目录。
    """
    path = TEMP_DIR / f"video-{task_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_disk_space() -> None:
    """检查磁盘可用空间，不足则拒绝任务。

    Raises:
        DiskFullError: 可用空间低于 ``min_free_gb``。
    """
    video = get_settings().video
    required = int(video.min_free_gb) * 1024**3
    free = disk_free_bytes(TEMP_DIR)
    if required and free < required:
        raise DiskFullError(
            f"磁盘可用空间不足（剩余 {free / 1024**3:.1f} GB，要求至少 {video.min_free_gb} GB），"
            "请清理 storage 目录后重试"
        )


def _max_file_bytes() -> int:
    """单文件大小上限（字节）。

    Returns:
        上限字节数。
    """
    return int(get_settings().video.max_file_mb) * 1024 * 1024


# ---------------------------------------------------------------- 清晰度选择


def _format_from_dict(data: dict[str, Any]) -> VideoFormat:
    """把清晰度字典还原为 VideoFormat（用于默认选择结果）。

    Args:
        data: 含契约字段的字典。

    Returns:
        VideoFormat。
    """
    return VideoFormat(
        format_id=str(data.get("format_id") or ""),
        quality=str(data.get("quality") or "unknown"),
        width=data.get("width"),
        height=data.get("height"),
        fps=data.get("fps"),
        bitrate=data.get("bitrate"),
        container=str(data.get("container") or "mp4"),
        size_bytes=data.get("size_bytes"),
        need_login=bool(data.get("need_login")),
        need_vip=bool(data.get("need_vip")),
        expire_at=data.get("expire_at"),
        note=data.get("note"),
        url=data.get("url"),
        audio_url=data.get("audio_url"),
    )


def select_format(item: VideoItem, format_id: str | None) -> VideoFormat:
    """按请求格式与默认策略选择清晰度。

    ``format_id`` 可能是前端从旧一次解析里带回来的 id（占位项、或平台改了 id 规则），
    因此命中项没有媒体地址时先尝试「同清晰度的可播放项」，仍不行才按
    VIP → 登录 → 无地址 的顺序给出明确错误。

    Args:
        item: 作品元数据。
        format_id: 用户选择的平台格式标识。

    Returns:
        可下载的 VideoFormat。

    Raises:
        LoginRequiredError: 只能选择需登录的清晰度。
        VipRequiredError: 只能选择会员清晰度。
        VideoParseError: 没有任何清晰度。
        DownloadFailedError: 选中的清晰度没有媒体地址。
    """
    fmt = item.format_by_id(format_id)
    if fmt is not None and not fmt.url:
        # 兼容旧 format_id / 占位项：同清晰度里挑一条真正有地址的流
        alternatives = [
            candidate
            for candidate in item.formats
            if candidate.url
            and not candidate.need_vip
            and not candidate.need_login
            and candidate.quality == fmt.quality
        ]
        if alternatives:
            fmt = max(
                alternatives,
                key=lambda candidate: (candidate.height or 0, candidate.bitrate or 0),
            )
    playable = [candidate for candidate in item.formats if candidate.url and not candidate.need_vip]
    if fmt is None:
        chosen = pick_default_format(
            playable or item.formats, get_settings().video.prefer_quality, allow_login=False
        )
        if chosen is not None:
            fmt = item.format_by_id(str(chosen.get("format_id"))) or _format_from_dict(chosen)
    if fmt is None:
        if any(item_format.need_login for item_format in item.formats):
            raise LoginRequiredError("该清晰度需要登录，请先在设置中填写对应平台的 Cookie")
        if any(item_format.need_vip for item_format in item.formats):
            raise VipRequiredError("该清晰度为会员专属，暂不支持下载")
        raise VideoParseError("没有可用的清晰度，请重新解析后再试")
    if fmt.need_vip:
        raise VipRequiredError("该清晰度为会员专属，暂不支持下载")
    if fmt.need_login:
        # 只按「选中项自身」判断：其它档位需要登录不代表这一档不能用
        raise LoginRequiredError("该清晰度需要登录，请先在设置中填写对应平台的 Cookie")
    if not fmt.url:
        raise DownloadFailedError("该清晰度没有可用的媒体地址，请选择其他清晰度后重试")
    return fmt


# ---------------------------------------------------------------- 基础下载


def _download_media(
    ctx: JobContext,
    url: str,
    dest: Path,
    *,
    low: int,
    high: int,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> Path:
    """下载一个媒体流（自动选择分片或单流）。

    Args:
        ctx: 任务上下文。
        url: 媒体地址。
        dest: 目标文件。
        low: 进度区间起点。
        high: 进度区间终点。
        timeout: 单次连接超时。
        headers: 平台媒体请求头（B站 CDN 强制校验 ``Referer`` + UA）。

    Returns:
        下载完成的文件路径。

    Raises:
        OutputTooLargeError: 超过单文件上限。
        DownloadFailedError: 重试耗尽仍失败。
    """
    max_bytes = _max_file_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    probe = net.probe_media(url, timeout=min(10.0, timeout), headers=headers)
    ctx.progress(min(high, max(low, 5)))
    if probe.total_size is not None and probe.total_size > max_bytes:
        raise OutputTooLargeError(
            f"文件体积约 {probe.total_size / 1024**2:.0f} MB，超过单文件上限 "
            f"{get_settings().video.max_file_mb} MB，请选择更低清晰度"
        )

    last_error: Exception | None = None
    for attempt in range(_TASK_ATTEMPTS):
        ctx.raise_if_cancelled()
        try:
            if probe.total_size and probe.accept_ranges:
                _download_segmented(
                    ctx, url, dest, probe.total_size, low, high, timeout, headers=headers
                )
            else:
                _download_single(
                    ctx, url, dest, probe.total_size, low, high, timeout, headers=headers
                )
            if not dest.exists() or dest.stat().st_size <= 0:
                raise DownloadFailedError("下载结果为空，请重试")
            if dest.stat().st_size > max_bytes:
                dest.unlink(missing_ok=True)
                raise OutputTooLargeError(
                    f"文件超过单文件上限 {get_settings().video.max_file_mb} MB，请选择更低清晰度"
                )
            return dest
        except TaskCancelled:
            raise
        except (DownloadFailedError, OSError) as exc:
            last_error = exc
            if attempt < _TASK_ATTEMPTS - 1:
                ctx.progress(min(high - 1, low + 1))
                _sleep_responsive(ctx, _TASK_BACKOFF_SEC[min(attempt, len(_TASK_BACKOFF_SEC) - 1)])
                continue
    raise DownloadFailedError(f"下载失败，已自动重试 2 次：{_safe_reason(last_error)}")


def _safe_reason(exc: Exception | None) -> str:
    """提取适合展示的失败原因（去掉可能的 URL / 堆栈）。

    Args:
        exc: 异常。

    Returns:
        截断后的原因文本。
    """
    if exc is None:
        return "网络异常"
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:160]


def _download_single(
    ctx: JobContext,
    url: str,
    dest: Path,
    total: int | None,
    low: int,
    high: int,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    """单流下载（Range 不可用时）。

    Args:
        ctx: 任务上下文。
        url: 媒体地址。
        dest: 目标文件。
        total: 总字节数（未知为 None）。
        low: 进度区间起点。
        high: 进度区间终点。
        timeout: 连接超时。
        headers: 平台媒体请求头。
    """
    progress = _Progress(ctx, low, high)
    downloaded = 0
    baseline = total or max(1, _max_file_bytes() // 20)

    def _on_chunk(size: int) -> None:
        """累计字节并推进进度。

        Args:
            size: 本次写入字节数。
        """
        nonlocal downloaded
        ctx.raise_if_cancelled()
        downloaded += size
        progress.set_fraction(min(0.98, downloaded / baseline))

    net.download_range(
        url,
        dest,
        timeout=timeout,
        max_bytes=_max_file_bytes(),
        on_chunk=_on_chunk,
        headers=headers,
    )
    progress.set_fraction(1.0)


def _seg_path(directory: Path, index: int, prefix: str) -> Path:
    """分片文件路径。

    Args:
        directory: 工作目录。
        index: 分片序号（0 起）。
        prefix: 文件名前缀（区分音视频流）。

    Returns:
        分片路径（``seg-0001.part``）。
    """
    return directory / f"{prefix}seg-{index + 1:04d}.part"


def _read_meta(path: Path) -> dict[str, Any] | None:
    """读取分片元数据（损坏时返回 None，不影响重下）。

    Args:
        path: ``meta.json`` 路径。

    Returns:
        元数据字典；缺失或损坏返回 None。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_meta(path: Path, url: str, total: int, segment_size: int, segments: int) -> None:
    """写入分片元数据（总大小 / 分片表 / 来源地址）。

    Args:
        path: ``meta.json`` 路径。
        url: 媒体地址。
        total: 文件总字节数。
        segment_size: 分片大小（字节）。
        segments: 分片数量。
    """
    payload = {
        "url": url,
        "total_size": total,
        "segment_size": segment_size,
        "segments": segments,
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 元数据只用于续传优化，写失败不应中断下载
        logger.debug("分片元数据写入失败：%s", path.name)


def _download_segmented(
    ctx: JobContext,
    url: str,
    dest: Path,
    total: int,
    low: int,
    high: int,
    timeout: float,
    *,
    prefix: str = "",
    headers: dict[str, str] | None = None,
) -> None:
    """Range 分片下载（支持断点续传）。

    Args:
        ctx: 任务上下文。
        url: 媒体地址。
        dest: 目标文件。
        total: 文件总字节数。
        low: 进度区间起点。
        high: 进度区间终点。
        timeout: 连接超时。
        prefix: 分片/元数据文件名前缀（音视频分流时区分）。
        headers: 平台媒体请求头。
    """
    video = get_settings().video
    segment_size = max(1, int(video.segment_size_mb)) * 1024 * 1024
    segments = max(1, math.ceil(total / segment_size))
    directory = dest.parent
    meta_path = directory / f"{prefix}meta.json"
    progress = _Progress(ctx, low, high)

    # meta.json 记录本次下载的 URL/总大小/分片表；总大小与旧记录不一致时说明
    # 媒体地址已变更（重新解析过），旧分片不可复用，整体清空重下。
    previous = _read_meta(meta_path)
    if previous is not None and int(previous.get("total_size") or 0) != total:
        _cleanup_segments(directory, max(segments, int(previous.get("segments") or 0)), prefix)
    _write_meta(meta_path, url, total, segment_size, segments)

    completed_bytes = sum(
        _completed_length(directory, index, prefix, total, segment_size) for index in range(segments)
    )
    progress.set_fraction(min(1.0, completed_bytes / total) if total else 0.0)

    counter_lock = threading.Lock()
    downloaded = [completed_bytes]

    def _on_segment_bytes(size: int) -> None:
        """更新分片进度。

        Args:
            size: 本次写入字节数。
        """
        with counter_lock:
            downloaded[0] = min(total, downloaded[0] + size)
            progress.set_fraction(downloaded[0] / total)

    def _fetch(index: int) -> int:
        """下载单个分片（含 3 次退避重试）。

        Args:
            index: 分片序号。

        Returns:
            写入字节数。

        Raises:
            DownloadFailedError: 重试耗尽。
            TaskCancelled: 任务取消。
        """
        start = index * segment_size
        end = min(total - 1, start + segment_size - 1)
        expected = end - start + 1
        path = _seg_path(directory, index, prefix)
        last: Exception | None = None
        for attempt in range(_SEGMENT_RETRIES):
            ctx.raise_if_cancelled()
            path.unlink(missing_ok=True)
            try:
                written = net.download_range(
                    url,
                    path,
                    start=start,
                    end=end,
                    timeout=timeout,
                    max_bytes=expected + 1024,
                    on_chunk=_on_segment_bytes,
                    headers=headers,
                )
                if written != expected:
                    raise DownloadFailedError(f"分片 {index + 1} 数据不完整")
                return written
            except TaskCancelled:
                raise
            except (DownloadFailedError, OSError) as exc:
                last = exc
                if attempt < _SEGMENT_RETRIES - 1:
                    _sleep_responsive(ctx, _SEGMENT_BACKOFF_SEC[min(attempt, 2)])
        raise DownloadFailedError(f"分片 {index + 1} 下载失败：{_safe_reason(last)}")

    workers = max(1, min(int(video.download_segments), segments))
    pending = [
        index
        for index in range(segments)
        if _completed_length(directory, index, prefix, total, segment_size)
        < min(segment_size, total - index * segment_size)
    ]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video-seg") as pool:
        futures = [pool.submit(_fetch, index) for index in pending]
        try:
            for future in futures:
                future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    # 合并分片
    expected_total = 0
    with dest.open("wb") as output:
        for index in range(segments):
            path = _seg_path(directory, index, prefix)
            if not path.exists():
                raise DownloadFailedError("分片文件缺失，请重试")
            expected = min(segment_size, total - index * segment_size)
            actual = path.stat().st_size
            if actual != expected:
                raise DownloadFailedError("分片数据不完整，请重试")
            with path.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            expected_total += actual
    if expected_total != total:
        dest.unlink(missing_ok=True)
        raise DownloadFailedError("下载结果大小与文件总大小不一致，请重试")

    _cleanup_segments(directory, segments, prefix)
    progress.set_fraction(1.0)


def _completed_length(
    directory: Path, index: int, prefix: str, total: int, segment_size: int
) -> int:
    """取一个分片当前已完成的字节数（用于断点续传跳过）。

    Args:
        directory: 工作目录。
        index: 分片序号。
        prefix: 文件名前缀。
        total: 文件总大小。
        segment_size: 分片大小。

    Returns:
        完整时返回该分片应有长度，否则 0。
    """
    path = _seg_path(directory, index, prefix)
    expected = min(segment_size, total - index * segment_size)
    try:
        if path.exists() and path.stat().st_size == expected:
            return expected
    except OSError:
        return 0
    return 0


def _cleanup_segments(directory: Path, segments: int, prefix: str) -> None:
    """删除已完成任务的分片与元数据文件。

    Args:
        directory: 工作目录。
        segments: 分片数量。
        prefix: 文件名前缀。
    """
    for index in range(segments):
        _seg_path(directory, index, prefix).unlink(missing_ok=True)
    (directory / f"{prefix}meta.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- FFmpeg


def _ffmpeg_or_error(purpose: str) -> Path:
    """取 FFmpeg 路径，缺失时给出安装指引。

    Args:
        purpose: 用途描述（用于错误文案）。

    Returns:
        FFmpeg 可执行文件路径。

    Raises:
        EngineUnavailableError: 未安装 FFmpeg。
    """
    executable = ffmpeg_path()
    if executable is None:
        raise EngineUnavailableError(
            f"缺少 FFmpeg，无法{purpose}。请将 FFmpeg 解压到 runtime/ffmpeg/bin/，"
            "或在 config.toml 的 [engines] 中配置 ffmpeg 路径后重试。"
        )
    return executable


def _run_ffmpeg(args: list[str], *, purpose: str) -> None:
    """执行 FFmpeg 并检查返回码。

    Args:
        args: ``-i`` 之后的参数列表（自动补 ``-y -hide_banner``）。
        purpose: 用途描述。

    Raises:
        EngineUnavailableError: 缺少 FFmpeg。
        DownloadFailedError: FFmpeg 执行失败或超时。
    """
    executable = _ffmpeg_or_error(purpose)
    code, _stdout, stderr = run_process(
        [str(executable), "-y", "-hide_banner", "-loglevel", "error", *args],
        timeout=_FFMPEG_TIMEOUT_SEC,
    )
    if code == -9:
        raise DownloadFailedError(f"{purpose}超时，请稍后重试")
    if code != 0:
        tail = " ".join((stderr or "").strip().splitlines()[-1:])[:160]
        raise DownloadFailedError(f"{purpose}失败：{tail or 'FFmpeg 返回错误'}")


def _merge_av(video_path: Path, audio_path: Path | None, output: Path) -> Path:
    """合并 DASH 音视频流（``-c copy``）。

    Args:
        video_path: 视频流文件。
        audio_path: 音频流文件（可为 None）。
        output: 输出 mp4 路径。

    Returns:
        输出路径。

    Raises:
        EngineUnavailableError: 缺少 FFmpeg。
        DownloadFailedError: 合并失败。
    """
    if audio_path is None:
        return video_path
    args = [
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(args, purpose="合并音视频")
    return output


def _extract_audio(source: Path, output: Path, output_format: str) -> Path:
    """从视频/音频容器中导出 mp3 / m4a。

    Args:
        source: 源文件（含音轨）。
        output: 输出路径。
        output_format: ``mp3`` 或 ``m4a``。

    Returns:
        输出路径。

    Raises:
        EngineUnavailableError: 缺少 FFmpeg。
        DownloadFailedError: 转码失败。
    """
    if output_format == "mp3":
        args = ["-i", str(source), "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(output)]
        _run_ffmpeg(args, purpose="导出 MP3")
        return output
    args = ["-i", str(source), "-vn", "-c:a", "copy", str(output)]
    _run_ffmpeg(args, purpose="导出 M4A")
    return output


def _remux_mp4(source: Path, output: Path) -> Path:
    """把非 mp4 容器重封装为 mp4（``-c copy``）。

    Args:
        source: 源文件。
        output: 输出路径。

    Returns:
        输出路径。

    Raises:
        EngineUnavailableError: 缺少 FFmpeg。
        DownloadFailedError: 重封装失败。
    """
    _run_ffmpeg(
        ["-i", str(source), "-c", "copy", "-movflags", "+faststart", str(output)],
        purpose="转换为 MP4",
    )
    return output


# ---------------------------------------------------------------- 图文


def _image_extension(url: str, content_type: str | None = None) -> str:
    """推断图片扩展名。

    Args:
        url: 图片地址。
        content_type: 响应 Content-Type（可选）。

    Returns:
        扩展名（不含点）。
    """
    content_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/heic": "heic",
    }
    if content_type:
        key = content_type.split(";")[0].strip().lower()
        if key in content_map:
            return content_map[key]
    suffix = Path(url.split("?")[0]).suffix.lstrip(".").lower()
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "heic"}:
        return "jpg" if suffix == "jpeg" else suffix
    return "jpg"


def _package_images(
    ctx: JobContext,
    item: VideoItem,
    workdir: Path,
    low: int,
    high: int,
    *,
    headers: dict[str, str] | None = None,
) -> Path:
    """下载图文作品并打包为 zip。

    Args:
        ctx: 任务上下文。
        item: 作品元数据。
        workdir: 工作目录。
        low: 进度区间起点。
        high: 进度区间终点。
        headers: 平台媒体请求头（图片 CDN 同样有防盗链）。

    Returns:
        zip 文件路径。

    Raises:
        VideoParseError: 没有图片。
        DownloadFailedError: 图片下载失败。
    """
    if not item.images:
        raise VideoParseError("图文作品没有可下载的图片")
    image_dir = workdir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = _max_file_bytes()
    progress = _Progress(ctx, low, high)
    files: list[Path] = []
    total = len(item.images)

    for index, image in enumerate(item.images):
        ctx.raise_if_cancelled()
        url = image.url
        ext = _image_extension(url)
        destination = image_dir / f"{index + 1:02d}.{ext}"
        last_error: Exception | None = None
        for attempt in range(_TASK_ATTEMPTS):
            ctx.raise_if_cancelled()
            try:
                net.download_range(
                    url,
                    destination,
                    timeout=30.0,
                    max_bytes=max_bytes,
                    on_chunk=lambda _size, _idx=index: progress.set_fraction(
                        (_idx + 0.5) / max(1, total)
                    ),
                    headers=headers,
                )
                files.append(destination)
                break
            except TaskCancelled:
                raise
            except (net.NetError, OSError) as exc:
                last_error = exc
                if attempt < _TASK_ATTEMPTS - 1:
                    _sleep_responsive(ctx, _SEGMENT_BACKOFF_SEC[min(attempt, 2)])
        else:
            raise DownloadFailedError(f"第 {index + 1} 张图片下载失败：{_safe_reason(last_error)}")
        progress.set_fraction((index + 1) / max(1, total))

    progress.set_percent(high)
    zip_path = workdir / "images.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            ctx.raise_if_cancelled()
            archive.write(path, arcname=path.name)
    return zip_path


# ---------------------------------------------------------------- 任务构造


def _image_job(
    ctx: JobContext,
    item: VideoItem,
    workdir: Path,
    *,
    headers: dict[str, str] | None = None,
) -> JobResult:
    """执行图文打包任务。

    Args:
        ctx: 任务上下文。
        item: 作品元数据。
        workdir: 工作目录。
        headers: 平台媒体请求头。

    Returns:
        任务产出。
    """
    ctx.progress(5)
    final = _package_images(ctx, item, workdir, 15, 92, headers=headers)
    ctx.progress(97)
    ext = "zip"
    name = naming.dedupe_name(
        naming.build_name(
            platform=item.platform,
            author=item.author.name,
            title=item.title,
            quality="图文",
            ext=ext,
        )
    )
    staged = stage_output(final)
    ctx.progress(99)
    return JobResult(path=staged, name=name, mime=naming.mime_for_ext(ext))


def make_download_job(
    item: VideoItem,
    *,
    format_id: str | None = None,
    output: str = "mp4",
) -> JobFunc:
    """构造下载任务函数（由 service 提交给任务中心）。

    Args:
        item: 已解析的作品元数据（可含媒体地址）。
        format_id: 用户选择的清晰度标识。
        output: ``mp4`` / ``mp3`` / ``m4a``（图文作品忽略，固定 zip）。

    Returns:
        符合 ``JobFunc`` 签名的闭包。
    """
    video_settings = get_settings().video
    output_format = (output or video_settings.prefer_format or "mp4").lower()
    if output_format not in {"mp4", "mp3", "m4a"}:
        output_format = "mp4"

    def job(ctx: JobContext) -> JobResult:
        """执行下载任务。

        Args:
            ctx: 任务上下文。

        Returns:
            任务产出。

        Raises:
            TaskCancelled: 任务被取消。
            ToolkitError: 下载/合并失败（带契约错误码）。
        """
        ctx.progress(1)
        _ensure_disk_space()
        workdir = workdir_for(ctx.task_id)
        acquired = False
        try:
            download_gate.acquire(ctx)
            acquired = True
            ctx.progress(3)
            # 平台媒体防盗链头（B站 CDN 缺少 Referer+UA 会直接 403），全部媒体请求复用
            media_headers = adapters.media_headers(item.platform)
            has_playable_video = any(
                candidate.url and not candidate.need_vip for candidate in item.formats
            )
            if item.images and (item.media_type == "image" or not has_playable_video):
                return _image_job(ctx, item, workdir, headers=media_headers)

            fmt = select_format(item, format_id)
            # 媒体地址可能是解析时生成的临时签名地址，下载前必须再走一次 SSRF 校验
            media_url = net.validate_media_url(fmt.url or "")
            audio_url = net.validate_media_url(fmt.audio_url) if fmt.audio_url else None
            ctx.progress(5)

            container = (fmt.container or "mp4").lower()
            if output_format == "mp4" and audio_url is None and container == "mp4":
                # 渐进式 mp4（音视频已封装）：无需 FFmpeg，直接落盘
                final = _download_media(
                    ctx, media_url, workdir / "video.mp4", low=15, high=90, headers=media_headers
                )
            else:
                video_path = _download_media(
                    ctx,
                    media_url,
                    workdir / "video.mp4",
                    low=15,
                    high=70 if audio_url else 88,
                    headers=media_headers,
                )
                audio_path: Path | None = None
                if audio_url:
                    audio_path = _download_media(
                        ctx,
                        audio_url,
                        workdir / "audio.m4a",
                        low=72,
                        high=88,
                        headers=media_headers,
                    )
                ctx.progress(90)
                if output_format == "mp4":
                    if audio_path is not None:
                        final = _merge_av(video_path, audio_path, workdir / "merged.mp4")
                    elif container != "mp4":
                        final = _remux_mp4(video_path, workdir / "remux.mp4")
                    else:
                        final = video_path
                else:
                    source = audio_path if audio_path is not None else video_path
                    if (
                        output_format == "m4a"
                        and audio_path is not None
                        and audio_path.suffix.lower() == ".m4a"
                    ):
                        # DASH 音频本身就是 m4a：直通，不重新编码
                        final = audio_path
                    else:
                        final = _extract_audio(
                            source, workdir / f"output.{output_format}", output_format
                        )

            ctx.progress(96)
            ext = final.suffix.lstrip(".").lower() or output_format
            name = naming.dedupe_name(
                naming.build_name(
                    platform=item.platform,
                    author=item.author.name,
                    title=item.title,
                    quality=fmt.quality,
                    ext=ext,
                )
            )
            staged = stage_output(final)
            ctx.progress(99)
            return JobResult(path=staged, name=name, mime=naming.mime_for_ext(ext))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if acquired:
                download_gate.release()

    return job


#: 语义化别名（部分调用方/测试使用 download_job 名称）
download_job = make_download_job


__all__ = [
    "DownloadGate",
    "download_gate",
    "download_job",
    "make_download_job",
    "select_format",
    "workdir_for",
]
