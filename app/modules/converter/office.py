"""LibreOffice headless 调用封装。

三条硬约束全部在此落实：
    NFR-DC-01  每次调用为独立子进程 + 超时强杀（防止 soffice 进程残留）
    NFR-DC-03  源文件名已由上层 UUID 化，杜绝参数注入
    NFR-DC-04  每个 worker 独占 ``-env:UserInstallation`` 目录（风险 R8）

关于 ``-env:UserInstallation``：LibreOffice 用该目录存放配置与锁文件，
并发进程共享同一目录时会随机出现「另一个实例正在运行」而直接退出，
表现为转换随机失败。因此必须为每个并发槽位分配独立目录。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from app.core.engines import engine_probe, profile_allocator, run_process
from app.core.errors import ConversionFailedError, EngineUnavailableError
from app.core.tasks import JobContext

logger = logging.getLogger(__name__)

# LibreOffice 的 --convert-to 目标标识
# 显式指定导出过滤器可以避免 LO 在多种同名过滤器之间选择错误
LO_FILTERS: Final[dict[str, str]] = {
    "pdf": "pdf",
    "docx": "docx:MS Word 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
    "html": "html:HTML (StarWriter)",
    "txt": "txt:Text (encoded):UTF8",
    "png": "png",
    "jpg": "jpg",
}


def _file_url(path: Path) -> str:
    """把本地路径转换为 LibreOffice 需要的 file:// URL。

    Windows 上必须是 ``file:///C:/...`` 形式（三个斜杠 + 正斜杠），
    直接传 ``C:\\...`` 会被 LO 解析成相对 URL 而静默失败。

    Args:
        path: 绝对路径。

    Returns:
        file:// 形式的 URL 字符串。
    """
    resolved = path.resolve()
    text = resolved.as_posix()
    if not text.startswith("/"):
        # Windows 盘符路径：C:/xxx → /C:/xxx
        text = f"/{text}"
    return f"file://{text}"


def _resolve_soffice() -> Path:
    """取 LibreOffice 可执行文件路径。

    Returns:
        可执行文件路径。

    Raises:
        EngineUnavailableError: 引擎缺失，错误信息中含安装指引。
    """
    status = engine_probe.status_of("libreoffice")
    if not status.path:
        raise EngineUnavailableError(
            "未检测到 LibreOffice 引擎，无法执行 Office 文档转换。"
            + (status.hint or "请在 config.toml [engines] 中配置 libreoffice 路径。")
        )
    return Path(status.path)


def convert_with_libreoffice(
    source: Path,
    target_format: str,
    workdir: Path,
    *,
    timeout_sec: int | None = None,
) -> Path:
    """调用 LibreOffice 完成一次转换。

    Args:
        source: 源文件路径。
        target_format: 目标格式 id（pdf / docx / xlsx / pptx / html / png / jpg）。
        workdir: 输出目录。
        timeout_sec: 超时秒数；None 时取配置中的 libreoffice_timeout_sec。

    Returns:
        产出的文件路径。

    Raises:
        EngineUnavailableError: LibreOffice 不可用。
        ConversionFailedError: 转换失败或未产出文件。
    """
    from app.core.config import get_settings

    soffice = _resolve_soffice()
    timeout = timeout_sec or get_settings().engines.libreoffice_timeout_sec

    convert_to = LO_FILTERS.get(target_format, target_format)
    outdir = workdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    profile_dir = profile_allocator.acquire()
    try:
        args = [
            str(soffice),
            "--headless",
            "--norestore",
            "--invisible",
            "--nolockcheck",
            "--nologo",
            "--nodefault",
            f"-env:UserInstallation={_file_url(profile_dir)}",
            "--convert-to",
            convert_to,
            "--outdir",
            str(outdir),
            str(source.resolve()),
        ]

        code, stdout, stderr = run_process(args, timeout=timeout)
        if code != 0:
            # LO 的报错常写在 stdout 上，两者都取
            detail = (stderr or stdout).strip().splitlines()
            reason = detail[-1] if detail else f"退出码 {code}"
            raise ConversionFailedError(
                f"LibreOffice 转换失败：{reason[:200]}。"
                "常见原因：源文件损坏、格式与扩展名不符、或文档包含不受支持的嵌入对象。"
            )
    finally:
        profile_allocator.release(profile_dir)

    # LO 会以「源文件主干 + 目标扩展名」命名输出，此处按实际文件查找更稳妥
    candidates = sorted(
        (p for p in outdir.iterdir() if p.is_file() and p.stem == source.stem),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise ConversionFailedError(
            "LibreOffice 未产出文件。请确认源文件未加密、未损坏；加密文档需要先去密码后再转换。"
        )
    return candidates[0]


def office_to_pdf(
    source: Path,
    workdir: Path,
    ctx: JobContext | None = None,
) -> Path:
    """Office 文档 → PDF 的便捷封装。

    Args:
        source: 源文件（docx / xlsx / pptx）。
        workdir: 输出目录。
        ctx: 任务上下文（可选，用于上报进度）。

    Returns:
        PDF 文件路径。

    Raises:
        EngineUnavailableError: LibreOffice 不可用。
        ConversionFailedError: 转换失败。
    """
    if ctx:
        ctx.progress(15)
    result = convert_with_libreoffice(source, "pdf", workdir)
    if ctx:
        ctx.progress(60)
    return result


def html_to_docx(source: Path, workdir: Path) -> Path:
    """HTML → DOCX（经 LibreOffice）。

    备选方案是解析 HTML 后用 python-docx 手工构造，但那样会丢失绝大部分样式；
    LO 的 HTML 导入过滤器虽然不完美，保真度仍明显更好（质量等级 B）。

    Args:
        source: HTML 文件路径。
        workdir: 输出目录。

    Returns:
        DOCX 文件路径。
    """
    return convert_with_libreoffice(source, "docx", workdir)


def html_to_xlsx(source: Path, workdir: Path) -> Path:
    """HTML → XLSX（经 LibreOffice 的表格识别）。

    Args:
        source: HTML 文件路径。
        workdir: 输出目录。

    Returns:
        XLSX 文件路径。
    """
    return convert_with_libreoffice(source, "xlsx", workdir)


def xlsx_to_docx(source: Path, workdir: Path) -> Path:
    """XLSX → DOCX（经 LibreOffice）。

    前端矩阵标注该路径为「经 HTML 中转」，实际由 LO 内部的过滤器链完成，
    对外表现一致，因此无需真的落一个中间 HTML 文件。

    Args:
        source: XLSX 文件路径。
        workdir: 输出目录。

    Returns:
        DOCX 文件路径。
    """
    return convert_with_libreoffice(source, "docx", workdir)


def office_version() -> str | None:
    """取 LibreOffice 版本号（引擎自检页展示）。

    Returns:
        版本字符串；不可用时返回 None。
    """
    return engine_probe.status_of("libreoffice").version


__all__ = [
    "LO_FILTERS",
    "convert_with_libreoffice",
    "html_to_docx",
    "html_to_xlsx",
    "office_to_pdf",
    "office_version",
    "xlsx_to_docx",
]
