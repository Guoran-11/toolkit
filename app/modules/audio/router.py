"""音频模块路由：``/api/audio/*``。

契约来源：``app/static/js/api.js`` 的 ``audioApi``。

    · ``POST /api/audio/probe``   —— multipart，字段名 ``files``，返回 ``{"results": [...]}``
      （前端 ``for (const item of data?.results || [])``）
    · ``POST /api/audio/convert`` —— multipart，字段名 ``files``，
      额外字段 ``output_format`` / ``bitrate`` / ``sample_rate`` / ``channels`` / ``keep_metadata``，
      返回**任务数组**

探测端点刻意不落长期盘：嗅探只需文件头，写完即删（规格书 §7.2 的明确要求）。
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import ClientIpDep, ConfigDep, GuardDep
from app.core.errors import ValidationError
from app.core.security import write_audit_log
from app.core.storage import safe_filename, save_stream, stage_output, upload_store
from app.core.tasks import JobContext, JobFunc, JobResult, TaskInput, get_task_manager
from app.modules.audio.decrypt import decrypt_if_needed
from app.modules.audio.probe import ALL_AUDIO_SUFFIXES, probe_file
from app.modules.audio.transcode import TranscodeOptions, copy_metadata, transcode

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])


# ---------------------------------------------------------------- 上传处理


def _close_upload(upload: UploadFile) -> None:
    """关闭上传句柄。

    Args:
        upload: 上传对象。
    """
    with contextlib.suppress(Exception):
        upload.file.close()


def _validate_suffix(name: str) -> str:
    """校验扩展名是否在音频白名单内。

    Args:
        name: 文件名。

    Returns:
        小写扩展名。

    Raises:
        ValidationError: 扩展名不在白名单内。
    """
    _, _, ext = (name or "").rpartition(".")
    suffix = ext.lower()
    if not suffix:
        raise ValidationError(f"文件 {name} 没有扩展名，无法判断音频格式。")
    if suffix not in ALL_AUDIO_SUFFIXES:
        raise ValidationError(
            f"不支持的音频扩展名 .{suffix}。支持的类型：{', '.join(sorted(ALL_AUDIO_SUFFIXES))}"
        )
    return suffix


# ---------------------------------------------------------------- 嗅探


@router.post("/api/audio/probe")
def audio_probe(
    settings: ConfigDep,
    _user: GuardDep,
    files: Annotated[list[UploadFile], File(description="待嗅探的音频文件")],
) -> dict[str, Any]:
    """嗅探音频格式（快速返回，不落长期盘）。

    Args:
        settings: 全局配置。
        _user: 鉴权主体。
        files: 上传的文件。

    Returns:
        ``{"results": [探测结果...]}``，字段与前端 audio.js 消费的结构一致。

    Raises:
        ValidationError: 未选择文件或数量超限。
    """
    if not files:
        raise ValidationError("请先选择音频文件。")
    if len(files) > settings.limits.max_batch_files:
        raise ValidationError(
            f"单次最多处理 {settings.limits.max_batch_files} 个文件，当前选择了 {len(files)} 个。"
        )

    results: list[dict[str, Any]] = []
    try:
        for upload in files:
            display_name = safe_filename(upload.filename or "audio")
            # 注意：整个单文件处理链（含扩展名校验）都必须在 try 内。
            # 早期版本把 _validate_suffix 放在 try 之外，导致"一个不支持的文件
            # 让整批探测 400"，前端会把同批的有效文件一并标为「无法识别」——
            # 这与契约（逐文件返回 decryptable 标记）相悖。
            temp_path: Path | None = None
            # 体积先用 multipart 解析出的原始大小兜底：扩展名校验发生在读取文件
            # 之前，一旦该步失败就拿不到落盘体积，若不兜底前端会把「文件体积」
            # 渲染成 0 B —— 而用户明明选了一个几 MB 的文件。
            size = int(getattr(upload, "size", 0) or 0)
            try:
                _validate_suffix(display_name)

                # 落到 uploads 目录做嗅探，随后立即删除（不落长期盘）
                temp_path = upload_store.allocate(display_name)
                size = save_stream(
                    upload.file, temp_path, max_bytes=settings.limits.max_audio_upload_bytes
                )
                result = probe_file(temp_path, display_name=display_name)
                payload = result.to_api()
                # 回填真实体积：前端用它渲染单文件体积
                payload.setdefault("size", size)
                results.append(payload)
            except BaseException as exc:  # noqa: BLE001
                # 单个文件失败不应让整批探测失败：前端会把该条标为不可用
                from app.core.errors import ToolkitError

                message = exc.detail if isinstance(exc, ToolkitError) else str(exc)[:200]
                results.append(
                    {
                        "name": display_name,
                        "size": size,
                        "detected_type": "unknown",
                        "detected_label": "无法识别",
                        "decryptable": False,
                        "container": None,
                        "duration_sec": None,
                        "note": message,
                        "platform": None,
                        "proprietary": False,
                    }
                )
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
    finally:
        for upload in files:
            _close_upload(upload)

    return {"results": results}


# ---------------------------------------------------------------- 转换任务


def make_audio_job(
    *,
    source_path: Path,
    display_name: str,
    options: TranscodeOptions,
) -> JobFunc:
    """构造音频转换任务函数。

    任务流程：
        1. 若为平台专有格式 → 解封装还原出中间音频（否则直通）；
        2. FFmpeg 转码为目标格式；
        3. mutagen 回写标签与封面（尽力而为）。

    Args:
        source_path: 已落盘的源文件。
        display_name: 用户可见的原始文件名。
        options: 转码参数。

    Returns:
        符合 ``JobFunc`` 签名的闭包。
    """

    def job(ctx: JobContext) -> JobResult:
        """执行音频转换任务。

        Args:
            ctx: 任务上下文。

        Returns:
            任务产出描述。
        """
        workdir = Path(tempfile.mkdtemp(prefix="audio-", dir=str(_temp_root())))
        try:
            ctx.progress(8)

            # 1) 解封装（若是专有格式）
            decrypted = decrypt_if_needed(source_path, workdir)
            if decrypted is not None:
                input_audio = decrypted.path
                metadata = decrypted.metadata
                cover = decrypted.cover
                logger.info("已解封装 %s → %s", display_name, decrypted.container)
            else:
                input_audio = source_path
                metadata = {}
                cover = None

            if ctx.cancelled:
                from app.core.tasks import TaskCancelled

                raise TaskCancelled()

            ctx.progress(25)

            # 2) 转码
            stem, dot, _ = display_name.rpartition(".")
            stem = stem if dot and stem else display_name
            output = workdir / f"{stem}.{options.suffix}"
            transcode(input_audio, output, options, ctx=ctx)

            # 3) 元数据与封面
            if options.keep_metadata and (metadata or cover):
                copy_metadata(output, metadata=metadata, cover=cover, ctx=ctx)

            ctx.progress(95)
            # 必须先把产物移出 workdir：任务函数返回后任务管理器才登记产物，
            # 而下面的 finally 会立刻删除整个 workdir（否则登记必然 FileNotFoundError）。
            staged = stage_output(output)
            return JobResult(path=staged, name=output.name, mime=_mime_for_suffix(options.suffix))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if source_path.exists() and source_path.parent == upload_store.root:
                source_path.unlink(missing_ok=True)

    return job


def _mime_for_suffix(suffix: str) -> str:
    """取扩展名对应的 MIME。

    Args:
        suffix: 扩展名（不含点）。

    Returns:
        MIME 字符串。
    """
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "m4a": "audio/mp4",
        "ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")


def _temp_root() -> Path:
    """取临时目录根。

    Returns:
        storage/temp 路径（已确保存在）。
    """
    from app.core.config import TEMP_DIR

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR


@router.post("/api/audio/convert")
def audio_convert(
    settings: ConfigDep,
    _user: GuardDep,
    ip: ClientIpDep,
    files: Annotated[list[UploadFile], File(description="待转换的音频文件")],
    output_format: Annotated[str, Form()] = "mp3",
    bitrate: Annotated[str, Form()] = "",
    sample_rate: Annotated[str, Form()] = "",
    channels: Annotated[str, Form()] = "",
    keep_metadata: Annotated[str, Form()] = "true",
) -> list[dict[str, Any]]:
    """提交音频转换任务。

    Args:
        settings: 全局配置。
        _user: 鉴权主体。
        ip: 客户端 IP（审计日志）。
        files: 上传的音频文件。
        output_format: 目标格式。
        bitrate: 比特率（kbps，可空）。
        sample_rate: 采样率（Hz，可空）。
        channels: 声道数（可空）。
        keep_metadata: 是否保留标签与封面。

    Returns:
        任务对象列表。

    Raises:
        ValidationError: 未选择文件或数量超限。
        ConversionFailedError: 输出格式不受支持。
    """
    if not files:
        raise ValidationError("请先选择音频文件。")
    if len(files) > settings.limits.max_batch_files:
        raise ValidationError(
            f"单次最多处理 {settings.limits.max_batch_files} 个文件，当前选择了 {len(files)} 个。"
        )

    options = TranscodeOptions.from_form(
        output_format=output_format,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        keep_metadata=keep_metadata,
    )

    # 阶段一：全部落盘校验
    staged: list[tuple[Path, str, int]] = []
    try:
        for upload in files:
            display_name = safe_filename(upload.filename or "audio")
            _validate_suffix(display_name)

            dest = upload_store.allocate(display_name)
            try:
                size = save_stream(upload.file, dest, max_bytes=settings.limits.max_audio_upload_bytes)
                if size == 0:
                    raise ValidationError(f"文件 {display_name} 内容为空。")
            except BaseException:
                dest.unlink(missing_ok=True)
                raise

            # 落盘后做一次真实性嗅探：拒绝伪装成音频的其它文件（NFR-04）
            probe = probe_file(dest, display_name=display_name)
            if probe.detected_type == "unknown" and not probe.proprietary:
                dest.unlink(missing_ok=True)
                raise ValidationError(f"文件 {display_name} 不是可识别的音频文件：{probe.note}")
            staged.append((dest, display_name, size))
    except BaseException:
        for path, _, _ in staged:
            path.unlink(missing_ok=True)
        for upload in files:
            _close_upload(upload)
        raise

    # 阶段二：提交任务
    manager = get_task_manager()
    results: list[dict[str, Any]] = []
    for path, display_name, size in staged:
        suffix = display_name.rpartition(".")[2].lower()
        record = manager.submit(
            module="audio",
            operation=f"{suffix}2{options.output_format}",
            inputs=[TaskInput(name=display_name, size=size, sha256=_sha256_prefix(path))],
            job=make_audio_job(source_path=path, display_name=display_name, options=options),
        )
        results.append(record.to_api())

    for upload in files:
        _close_upload(upload)

    write_audit_log(
        {
            "action": "audio.convert",
            "ip": ip,
            "output_format": options.output_format,
            "files": [name for _, name, _ in staged],
            "count": len(staged),
        }
    )
    return results


def _sha256_prefix(path: Path) -> str:
    """计算文件 SHA256 前 8 位。

    Args:
        path: 文件路径。

    Returns:
        十六进制前缀。
    """
    from app.core.storage import sha256_prefix

    return sha256_prefix(path)


__all__ = ["router"]
