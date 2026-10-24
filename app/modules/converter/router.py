"""转换中心路由：``/api/convert*``。

契约来源：``app/static/js/api.js`` 的 ``api.convert`` / ``api.matrix`` / ``api.batchZip``。

关键约定（勿改）：
    1. 上传使用 multipart，文件字段名为 ``files``（前端 ``form.append('files', ...)``）。
    2. ``params`` 是以 **JSON 字符串** 形式提交的（前端 ``JSON.stringify`` 后再 FormData）。
    3. ``POST /api/convert`` 返回**任务数组**，前端会做 ``Array.isArray(data) ? data : [data]``。
    4. 路由函数声明为同步 ``def``：文件落盘是阻塞 IO，交给 FastAPI 的线程池执行，
       避免拖住事件循环（在线程池里跑反而是同步代码更简单可靠）。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response

from app.api.deps import ClientIpDep, ConfigDep, GuardDep
from app.core.config import Settings
from app.core.engines import engine_probe
from app.core.errors import (
    NoUsableOutputError,
    UnsupportedFormatError,
    UnsupportedPairError,
    ValidationError,
)
from app.core.security import write_audit_log
from app.core.storage import (
    OutputFile,
    assert_format_allowed,
    new_id,
    registry,
    safe_filename,
    save_stream,
    sha256_prefix,
    upload_store,
)
from app.modules.converter.base import ConversionParams
from app.modules.converter.matrix import (
    SOURCE_MAGIC_KINDS,
    SOURCE_SUFFIXES,
    TARGET_FORMATS,
    format_from_suffix,
    is_supported,
    lookup,
    matrix_api_payload,
)
from app.modules.converter.service import submit_conversion

logger = logging.getLogger(__name__)

router = APIRouter(tags=["converter"])

# PDF 工具集支持的算子
PDF_OPERATIONS = frozenset({"merge", "split", "rotate", "encrypt", "decrypt", "extract"})


# ---------------------------------------------------------------- 矩阵


@router.get("/api/convert/matrix")
def get_matrix(settings: ConfigDep, _user: GuardDep) -> dict[str, Any]:
    """返回格式支持矩阵与引擎状态。

    前端 converter.js 优先使用本端点的矩阵（后端可按引擎可用性动态禁用路径），
    接口不可用时回退到本地静态定义。

    Args:
        settings: 全局配置。
        _user: 鉴权主体（未使用，仅用于强制鉴权）。

    Returns:
        含 ``sources`` / ``targets`` / ``matrix`` / ``engines`` / ``limits`` 的字典。
    """
    payload = matrix_api_payload()
    payload["engines"] = engine_probe.api_payload()
    payload["limits"] = {
        "max_upload_mb": settings.limits.max_upload_mb,
        "max_batch_files": settings.limits.max_batch_files,
    }
    return payload


# ---------------------------------------------------------------- 提交转换


def _parse_params(raw: str | None) -> ConversionParams:
    """解析前端提交的参数。

    前端可能提交 JSON 字符串、空串或非法 JSON。参数问题不应让任务失败，
    因此解析失败一律回退到默认参数。

    Args:
        raw: 表单中的 ``params`` 字段。

    Returns:
        ConversionParams 实例。
    """
    if not raw:
        return ConversionParams()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.info("params 不是合法 JSON，已使用默认参数：%s", raw[:120])
        return ConversionParams()
    if not isinstance(parsed, dict):
        return ConversionParams()
    return ConversionParams.from_raw(parsed)


def _validate_formats(source_format: str, target_format: str) -> tuple[str, str]:
    """校验源/目标格式标识。

    Args:
        source_format: 源格式 id。
        target_format: 目标格式 id。

    Returns:
        ``(source, target)`` 规范化后的小写标识。

    Raises:
        ValidationError: 标识缺失或非法。
        UnsupportedPairError: 组合不受支持。
    """
    source = (source_format or "").strip().lower()
    target = (target_format or "").strip().lower()

    if source not in SOURCE_SUFFIXES:
        raise ValidationError(f"未知的源格式：{source_format or '(空)'}")
    if target not in TARGET_FORMATS:
        raise ValidationError(f"未知的目标格式：{target_format or '(空)'}")
    if source == target:
        raise ValidationError("源格式与目标格式相同，无需转换。")

    if not is_supported(source, target):
        cell = lookup(source, target)
        reason = (cell.note if cell else "") or "该转换方向不在支持范围内"
        raise UnsupportedPairError(f"不支持 {source.upper()} → {target.upper()}：{reason}")

    return source, target


def _persist_and_validate(
    upload: UploadFile,
    *,
    source_format: str,
    settings: Settings,
) -> tuple[Path, str, int]:
    """把上传文件落盘并做类型校验。

    Args:
        upload: 上传的文件。
        source_format: 源格式 id（决定白名单）。
        settings: 全局配置。

    Returns:
        ``(磁盘路径, 清洗后的显示名, 字节数)``。

    Raises:
        PayloadTooLargeError: 超过体积上限。
        UnsupportedFormatError: 扩展名或 magic bytes 不符合白名单。
    """
    display_name = safe_filename(upload.filename or "upload")

    # 扩展名必须与声明的源格式兼容；前端选择框已限制，但不信任客户端
    declared = format_from_suffix(display_name)
    if declared != source_format:
        raise UnsupportedFormatError(
            f"文件 {display_name} 的扩展名与所选源格式（{source_format.upper()}）不符。"
            "请确认左侧「源格式」选择正确。"
        )

    dest = upload_store.allocate(display_name)
    try:
        size = save_stream(
            upload.file,
            dest,
            max_bytes=settings.limits.max_upload_bytes,
        )
        if size == 0:
            raise ValidationError(f"文件 {display_name} 内容为空。")

        assert_format_allowed(
            dest,
            allowed_kinds=SOURCE_MAGIC_KINDS.get(source_format, ()),
            allowed_suffixes=SOURCE_SUFFIXES.get(source_format, ()),
            hint="请确认文件未损坏，且未被重命名伪装扩展名。",
        )
    except BaseException:
        # 校验失败时不留垃圾文件（NFR-03）
        dest.unlink(missing_ok=True)
        raise

    return dest, display_name, size


@router.post("/api/convert")
def create_conversion(
    settings: ConfigDep,
    _user: GuardDep,
    ip: ClientIpDep,
    files: Annotated[list[UploadFile], File(description="待转换的文件，支持多选")],
    source_format: Annotated[str, Form()] = "",
    target_format: Annotated[str, Form()] = "",
    params: Annotated[str, Form()] = "{}",
) -> list[dict[str, Any]]:
    """提交转换任务（单文件或批量）。

    处理顺序刻意设计为「先全部落盘校验，再统一提交任务」：
    若第 5 个文件类型非法，前 4 个不会白白产生任务，用户看到的是一次干净的失败。

    Args:
        settings: 全局配置。
        _user: 鉴权主体。
        ip: 客户端 IP（写入审计日志）。
        files: 上传的文件列表。
        source_format: 源格式 id。
        target_format: 目标格式 id。
        params: JSON 字符串形式的转换参数。

    Returns:
        任务对象列表（每个文件一个任务）。

    Raises:
        ValidationError: 文件数量或参数非法。
        UnsupportedPairError: 转换方向不受支持。
    """
    if not files:
        raise ValidationError("请先选择要转换的文件。")
    if len(files) > settings.limits.max_batch_files:
        raise ValidationError(
            f"单次最多上传 {settings.limits.max_batch_files} 个文件，当前选择了 {len(files)} 个。"
        )

    source, target = _validate_formats(source_format, target_format)
    conversion_params = _parse_params(params)

    # 阶段一：全部落盘 + 校验。任一失败则整体回滚，不产生任何任务。
    staged: list[tuple[Path, str, int]] = []
    try:
        for upload in files:
            staged.append(_persist_and_validate(upload, source_format=source, settings=settings))
    except BaseException:
        for path, _, _ in staged:
            path.unlink(missing_ok=True)
        for upload in files:
            _close_upload(upload)
        raise

    # 阶段二：提交任务。此后文件的清理职责移交给任务本身（见 make_conversion_job）。
    results: list[dict[str, Any]] = []
    for path, display_name, size in staged:
        results.append(
            submit_conversion(
                source_path=path,
                source_format=source,
                target_format=target,
                display_name=display_name,
                params=conversion_params,
                file_size=size,
                sha256_prefix=sha256_prefix(path),
            )
        )

    for upload in files:
        _close_upload(upload)

    write_audit_log(
        {
            "action": "convert.submit",
            "ip": ip,
            "operation": f"{source}2{target}",
            "files": [name for _, name, _ in staged],
            "count": len(staged),
        }
    )
    return results


def _close_upload(upload: UploadFile) -> None:
    """关闭上传文件的底层句柄。

    不关闭会让 SpooledTemporaryFile 在临时目录里堆积，直到 GC 才释放。

    Args:
        upload: 上传对象。
    """
    with contextlib.suppress(Exception):
        upload.file.close()


# ---------------------------------------------------------------- 批量打包


def _build_zip(entries: list[tuple[str, Path]]) -> bytes:
    """把多个文件打包为 ZIP（内存中构造）。

    说明：批量结果通常是若干 MB 级文档，整体放入内存可接受；
    若未来出现 GB 级批量场景，应改为流式 ``StreamingResponse`` + 临时文件。

    Args:
        entries: ``(压缩包内文件名, 磁盘路径)`` 列表。

    Returns:
        ZIP 字节流。
    """
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in entries:
            # 同名文件加序号后缀，避免压缩包内出现重复条目
            candidate = name
            counter = 2
            while candidate in used:
                stem, dot, ext = name.rpartition(".")
                candidate = f"{stem}({counter}).{ext}" if dot else f"{name}({counter})"
                counter += 1
            used.add(candidate)
            archive.write(path, arcname=candidate)
    return buffer.getvalue()


@router.post("/api/convert/batch-zip")
def batch_zip(
    payload: dict[str, Any],
    settings: ConfigDep,
    _user: GuardDep,
) -> Response:
    """把多个任务的产物打包为 ZIP 下载。

    Args:
        payload: 请求体，形如 ``{"task_ids": ["...", "..."]}``。
        settings: 全局配置。
        _user: 鉴权主体。

    Returns:
        ZIP 文件的二进制响应。

    Raises:
        ValidationError: 未提供任务 ID 或数量超限。
        NoUsableOutputError: 全部任务均无可用产物（HTTP 409）。
    """
    from app.core.tasks import get_task_manager

    task_ids = payload.get("task_ids") or []
    if not isinstance(task_ids, list) or not task_ids:
        raise ValidationError("请提供需要打包的任务列表（task_ids）。")
    if len(task_ids) > settings.limits.max_batch_files:
        raise ValidationError(f"单次最多打包 {settings.limits.max_batch_files} 个结果。")

    manager = get_task_manager()
    entries: list[tuple[str, Path]] = []
    skipped: list[str] = []

    for task_id in task_ids:
        try:
            record = manager.get(str(task_id))
        except Exception:  # noqa: BLE001 - 任务不存在时跳过而非整体失败
            skipped.append(str(task_id))
            continue
        output = record.output or {}
        file_id = output.get("file_id")
        if not isinstance(file_id, str):
            skipped.append(str(task_id))
            continue
        try:
            registered: OutputFile = registry.get(file_id)
        except Exception:  # noqa: BLE001
            skipped.append(str(task_id))
            continue
        entries.append((registered.name, Path(registered.path)))

    if not entries:
        # 语义纠正：这是「所选资源当前不可用」的状态冲突，不是请求参数格式错误，
        # 因此用 409 而非 422。前端只需提示用户重新选择，无需重新组织请求体。
        raise NoUsableOutputError(
            "所选任务均无可用产物（可能已过期或被清理）。" + (f" 跳过 {len(skipped)} 项。" if skipped else "")
        )

    if skipped:
        entries.append(
            (
                "_跳过说明.txt",
                _write_temp_text(f"以下 {len(skipped)} 个任务没有可用产物，已跳过：\n" + "\n".join(skipped)),
            )
        )

    data = _build_zip(entries)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="toolkit-batch.zip"'},
    )


def _write_temp_text(content: str) -> Path:
    """把说明文本写入临时文件（供打包使用）。

    Args:
        content: 文本内容。

    Returns:
        临时文件路径。
    """
    from app.core.config import TEMP_DIR

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMP_DIR / f"note-{new_id()}.txt"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------- PDF 工具集


@router.post("/api/convert/pdf-tools")
def pdf_tools(
    settings: ConfigDep,
    _user: GuardDep,
    ip: ClientIpDep,
    files: Annotated[list[UploadFile], File(description="待处理的 PDF")],
    operation: Annotated[str, Form()] = "merge",
    params: Annotated[str, Form()] = "{}",
) -> dict[str, Any]:
    """PDF 工具集：合并 / 拆分 / 旋转 / 加密 / 去密码 / 提取页面（FR-DC-08）。

    这些操作都是毫秒到秒级（pypdf 纯 Python，无外部引擎），
    因此**不经任务队列**，直接同步执行并返回一个已完成的终态任务对象 ——
    与前端 mock.mdExport 中 HTML 导出的返回形态保持一致。

    Args:
        settings: 全局配置。
        _user: 鉴权主体。
        ip: 客户端 IP。
        files: 上传的 PDF 文件。
        operation: 算子名。
        params: JSON 字符串参数（如 ``{"pages": "1-3", "password": "..."}``）。

    Returns:
        终态任务对象。

    Raises:
        ValidationError: 算子或参数非法。
        ConversionFailedError: 处理失败。
    """
    from app.core.tasks import utc_now_iso
    from app.modules.converter import pdf as pdf_ops

    op = (operation or "").strip().lower()
    if op not in PDF_OPERATIONS:
        raise ValidationError(f"不支持的 PDF 操作：{operation}。可选：{', '.join(sorted(PDF_OPERATIONS))}")
    if not files:
        raise ValidationError("请先选择 PDF 文件。")

    raw_params: dict[str, Any] = {}
    if params:
        try:
            parsed = json.loads(params)
            if isinstance(parsed, dict):
                raw_params = parsed
        except json.JSONDecodeError:
            raise ValidationError("params 不是合法的 JSON。") from None

    if op == "merge" and len(files) < 2:
        raise ValidationError("合并至少需要 2 个 PDF 文件。")

    workdir = Path(tempfile.mkdtemp(prefix="pdf-tools-", dir=str(_temp_root())))
    staged_inputs: list[tuple[Path, str, int]] = []
    try:
        for upload in files:
            staged_inputs.append(_persist_and_validate(upload, source_format="pdf", settings=settings))

        sources = [item[0] for item in staged_inputs]
        pages_spec = str(raw_params.get("page_range") or raw_params.get("pages") or "all")
        rotate = int(raw_params.get("rotate") or 0)
        password = str(raw_params.get("password") or "")

        if op == "merge":
            output = pdf_ops.pdf_tool_merge(sources, workdir / "merged.pdf")
            product_name = "merged.pdf"
        elif op in {"split", "extract"}:
            output = pdf_ops.pdf_tool_extract_pages(
                sources[0],
                workdir / "pages.pdf",
                page_spec=pages_spec,
                rotate=rotate,
                password=password or None,
            )
            product_name = (
                f"{Path(safe_filename(staged_inputs[0][1])).stem}-{pages_spec.replace(',', '_')}.pdf"
            )
        elif op == "rotate":
            output = pdf_ops.pdf_tool_extract_pages(
                sources[0], workdir / "rotated.pdf", page_spec="all", rotate=rotate, password=password or None
            )
            product_name = f"{Path(safe_filename(staged_inputs[0][1])).stem}-rotated.pdf"
        elif op == "encrypt":
            key = str(raw_params.get("user_password") or raw_params.get("password") or "")
            output = pdf_ops.pdf_tool_encrypt(sources[0], workdir / "encrypted.pdf", user_password=key)
            product_name = f"{Path(safe_filename(staged_inputs[0][1])).stem}-encrypted.pdf"
        else:  # decrypt
            output = pdf_ops.pdf_tool_decrypt(sources[0], workdir / "decrypted.pdf", password=password)
            product_name = f"{Path(safe_filename(staged_inputs[0][1])).stem}-decrypted.pdf"

        registered = registry.register(
            output, name=product_name, mime="application/pdf", ttl_hours=settings.limits.output_ttl_hours
        )
        write_audit_log({"action": f"pdf.{op}", "ip": ip, "files": [n for _, n, _ in staged_inputs]})

        return {
            "task_id": new_id("t"),
            "module": "converter",
            "operation": f"pdf_{op}",
            "status": "success",
            "progress": 100,
            "inputs": [{"name": name, "size": size, "sha256": ""} for _, name, size in staged_inputs],
            "output": registered.to_api(),
            "error": None,
            "error_code": None,
            "created_at": utc_now_iso(),
            "started_at": utc_now_iso(),
            "finished_at": utc_now_iso(),
            "expire_at": None,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        for path, _, _ in staged_inputs:
            path.unlink(missing_ok=True)
        for upload in files:
            with contextlib.suppress(Exception):
                upload.file.close()


def _temp_root() -> Path:
    """取临时目录根（不存在则创建）。

    Returns:
        storage/temp 路径。
    """
    from app.core.config import TEMP_DIR

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR


__all__ = ["router"]
