"""转换路径分派与任务封装。

本模块是「矩阵」与「实现」之间的唯一桥梁：
    matrix.py 声明「哪些组合受支持、质量等级如何」
    service.py 负责「把某个组合真正跑起来」

每条路径的实现函数都是纯函数（输入路径 → 产出路径），
不依赖 FastAPI、不读写数据库，因此可以在测试中直接调用。
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from app.core.config import get_settings
from app.core.errors import ConversionFailedError, UnsupportedPairError
from app.core.storage import OutputFile, stage_output, upload_store
from app.core.tasks import JobContext, JobFunc, JobResult, TaskInput, get_task_manager
from app.modules.converter.base import ConversionOutcome, ConversionParams, ConversionRequest
from app.modules.converter.documents import (
    docx_to_html,
    docx_to_xlsx,
    pptx_to_docx,
    pptx_to_xlsx,
    xlsx_to_html,
)
from app.modules.converter.html2pdf import render_html_to_image, render_html_to_pdf
from app.modules.converter.matrix import TARGET_MIME, is_supported, lookup
from app.modules.converter.office import office_to_pdf
from app.modules.converter.pdf import (
    images_to_pptx,
    images_to_zip,
    pdf_to_docx,
    pdf_to_html,
    pdf_to_images,
    pdf_to_pptx,
    pdf_to_xlsx,
)

logger = logging.getLogger(__name__)

# 中间产物目录名（位于任务 workdir 内，随 workdir 一起回收）
_INTERMEDIATE_DIR: Final[str] = "_intermediate"


# ---------------------------------------------------------------- 内部工具


def _mime_for(target: str) -> str:
    """取目标格式的 MIME。

    Args:
        target: 目标格式 id。

    Returns:
        MIME 字符串；未知格式回退为二进制流。
    """
    return TARGET_MIME.get(target, "application/octet-stream")


def _intermediate(workdir: Path) -> Path:
    """取本次任务的中间产物目录（不存在则创建）。

    Args:
        workdir: 任务临时目录。

    Returns:
        中间产物目录路径。
    """
    path = workdir / _INTERMEDIATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inside(path: Path, root: Path) -> bool:
    """判断 ``path`` 是否位于 ``root`` 目录之内。

    复用 ``security.assert_within`` 的归一化比较（含符号链接解析），保证与
    路径穿越防护用同一套判定语义。

    Args:
        path: 待判断的文件路径。
        root: 参照目录。

    Returns:
        位于目录内返回 True；越界或路径异常返回 False。
    """
    from app.core.errors import ForbiddenError
    from app.core.security import assert_within

    try:
        assert_within(path, root)
    except ForbiddenError:
        return False
    except OSError:  # pragma: no cover - 极端路径长度/权限异常
        return False
    return True


def _stage(path: Path) -> Path:
    """把产出文件从任务目录搬到临时区，以便安全回收整个任务目录。

    时序问题：任务函数返回后，任务管理器才去做产物登记（把文件移入 outputs）。
    若任务函数在返回前删除整个 workdir，产出文件会连带被删。
    因此先把产出挪到 storage/temp 根目录（不在任何 workdir 内）再清理 workdir。

    实现见 :func:`app.core.storage.stage_output`（音频 / Markdown 模块共用同一逻辑）。

    Args:
        path: 产出文件路径。

    Returns:
        搬移后的路径。
    """
    return stage_output(path)


def _require_office_docx(source: Path) -> None:
    """确认 DOCX 是 OOXML 而非旧版 .doc 伪装。

    Args:
        source: 文件路径。

    Raises:
        ConversionFailedError: 实为旧版格式。
    """
    from app.core.storage import read_head, sniff_type

    kind = sniff_type(read_head(source))
    if kind == "ole":
        raise ConversionFailedError(
            "该文件实际为旧版 .doc / .xls / .ppt 格式（复合文档），并非 OOXML。"
            "请先用 Office 另存为 .docx / .xlsx / .pptx 后再上传。"
        )


# ---------------------------------------------------------------- 各路径实现


def _html_to_pdf(req: ConversionRequest) -> ConversionOutcome:
    """HTML → PDF（Playwright，A 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    path = render_html_to_pdf(req.source_path, req.workdir, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(path), name=req.output_name("pdf"), mime=_mime_for("pdf"))


def _html_to_png(req: ConversionRequest) -> ConversionOutcome:
    """HTML → PNG（Chromium 截图，A 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    path = render_html_to_image(req.source_path, req.workdir, params=req.params, ctx=req.ctx)
    suffix = path.suffix.lstrip(".")
    return ConversionOutcome(path=_stage(path), name=req.output_name(suffix), mime=_mime_for("png"))


def _html_to_docx(req: ConversionRequest) -> ConversionOutcome:
    """HTML → DOCX（经 LibreOffice，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    from app.modules.converter.office import html_to_docx

    path = html_to_docx(req.source_path, req.workdir)
    return ConversionOutcome(path=_stage(path), name=req.output_name("docx"), mime=_mime_for("docx"))


def _html_to_xlsx(req: ConversionRequest) -> ConversionOutcome:
    """HTML → XLSX（经 LibreOffice 的表格识别，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    from app.modules.converter.office import html_to_xlsx

    path = html_to_xlsx(req.source_path, req.workdir)
    return ConversionOutcome(path=_stage(path), name=req.output_name("xlsx"), mime=_mime_for("xlsx"))


def _office_to_pdf(req: ConversionRequest) -> ConversionOutcome:
    """DOCX / XLSX / PPTX → PDF（LibreOffice，A 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    path = office_to_pdf(req.source_path, req.workdir, req.ctx)
    return ConversionOutcome(path=_stage(path), name=req.output_name("pdf"), mime=_mime_for("pdf"))


def _office_to_images_zip(req: ConversionRequest) -> ConversionOutcome:
    """DOCX / XLSX / PPTX → 图片（经 PDF 中转，C 类，多页打包为 ZIP）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    intermediate = _intermediate(req.workdir)

    ctx = req.ctx
    pdf_path = office_to_pdf(req.source_path, intermediate, ctx)

    # 源格式自身的排版 DPI 无意义（是矢量），直接采用用户选择的输出 DPI
    image_params = ConversionParams(
        dpi=req.params.effective_dpi(),
        image_format=req.params.image_format,
        quality=req.params.quality,
        page_range="all",
    )
    images = pdf_to_images(pdf_path, intermediate / "pages", params=image_params, ctx=ctx)
    suffix = "jpg" if image_params.image_format.lower() in {"jpg", "jpeg"} else "png"

    if len(images) == 1:
        # 单页文档不必打包，直接给图片更友好
        staged = _stage(images[0])
        return ConversionOutcome(
            path=staged,
            name=req.output_name(suffix),
            mime="image/jpeg" if suffix == "jpg" else "image/png",
            extra_notes=["源文档仅 1 页，已直接输出图片而非压缩包。"],
        )

    archive = _stage(images_to_zip(images, intermediate, stem=f"{req.stem()}-pages"))
    return ConversionOutcome(
        path=archive,
        name=f"{req.stem()}-pages.zip",
        mime="application/zip",
        extra_notes=[f"共 {len(images)} 页，已打包为 ZIP。"],
    )


def _office_to_pptx(req: ConversionRequest) -> ConversionOutcome:
    """DOCX / XLSX → PPTX（每页一张图，C 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。

    Raises:
        ConversionFailedError: 中转失败。
    """
    _require_office_docx(req.source_path)
    intermediate = _intermediate(req.workdir)

    pdf_path = office_to_pdf(req.source_path, intermediate, req.ctx)
    images = pdf_to_images(
        pdf_path,
        intermediate / "pages",
        params=ConversionParams(dpi=150, image_format="png", page_range="all"),
        ctx=req.ctx,
    )
    output = req.workdir / "output.pptx"
    images_to_pptx(images, output, params=req.params)
    return ConversionOutcome(
        path=_stage(output),
        name=req.output_name("pptx"),
        mime=_mime_for("pptx"),
        extra_notes=["输出为每页一张图片的幻灯片，文字不可编辑（矩阵 C 类）。"],
    )


def _pptx_to_docx(req: ConversionRequest) -> ConversionOutcome:
    """PPTX → DOCX（备注 + 缩略图，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    output = req.workdir / "output.docx"
    pptx_to_docx(req.source_path, output, _intermediate(req.workdir), params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("docx"), mime=_mime_for("docx"))


def _pptx_to_html(req: ConversionRequest) -> ConversionOutcome:
    """PPTX → HTML（逐页图片，C 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    intermediate = _intermediate(req.workdir)
    from app.modules.converter.documents import _wrap_html_document

    pdf_path = office_to_pdf(req.source_path, intermediate, req.ctx)
    images = pdf_to_images(
        pdf_path,
        intermediate / "pages",
        params=ConversionParams(dpi=110, image_format="png", page_range="all"),
        ctx=req.ctx,
    )

    # 图片以 Base64 内嵌，保证导出的 HTML 是自包含单文件
    import base64

    sections: list[str] = []
    for index, image in enumerate(images, start=1):
        payload = base64.b64encode(image.read_bytes()).decode("ascii")
        sections.append(
            f'<h2>第 {index} 页</h2>\n<img alt="第 {index} 页" src="data:image/png;base64,{payload}">'
        )

    html = _wrap_html_document(
        req.stem(),
        "\n".join(sections),
        note="由 PPTX 转换而来，每页为一张图片，文字不可选中。",
    )
    output = req.workdir / "output.html"
    output.write_text(html, encoding="utf-8")
    return ConversionOutcome(path=_stage(output), name=req.output_name("html"), mime=_mime_for("html"))


def _docx_to_html(req: ConversionRequest) -> ConversionOutcome:
    """DOCX → HTML（mammoth，A 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    output = req.workdir / "output.html"
    docx_to_html(req.source_path, output, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("html"), mime=_mime_for("html"))


def _docx_to_xlsx(req: ConversionRequest) -> ConversionOutcome:
    """DOCX → XLSX（表格提取，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    output = req.workdir / "output.xlsx"
    docx_to_xlsx(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("xlsx"), mime=_mime_for("xlsx"))


def _pptx_to_xlsx(req: ConversionRequest) -> ConversionOutcome:
    """PPTX → XLSX（表格提取，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    output = req.workdir / "output.xlsx"
    pptx_to_xlsx(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("xlsx"), mime=_mime_for("xlsx"))


def _xlsx_to_html(req: ConversionRequest) -> ConversionOutcome:
    """XLSX → HTML（openpyxl，A 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    output = req.workdir / "output.html"
    xlsx_to_html(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("html"), mime=_mime_for("html"))


def _xlsx_to_docx(req: ConversionRequest) -> ConversionOutcome:
    """XLSX → DOCX（经 LibreOffice，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    _require_office_docx(req.source_path)
    from app.modules.converter.office import xlsx_to_docx

    intermediate = _intermediate(req.workdir)
    path = xlsx_to_docx(req.source_path, intermediate)
    return ConversionOutcome(path=_stage(path), name=req.output_name("docx"), mime=_mime_for("docx"))


def _pdf_to_images_zip(req: ConversionRequest) -> ConversionOutcome:
    """PDF → 图片（PyMuPDF，A 类，多页打包为 ZIP）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    images = pdf_to_images(req.source_path, req.workdir, params=req.params, ctx=req.ctx)
    suffix = "jpg" if req.params.image_format.lower() in {"jpg", "jpeg"} else "png"

    if len(images) == 1:
        return ConversionOutcome(
            path=_stage(images[0]),
            name=req.output_name(suffix),
            mime="image/jpeg" if suffix == "jpg" else "image/png",
            extra_notes=["源 PDF 仅 1 页，已直接输出图片。"],
        )

    archive = _stage(images_to_zip(images, _intermediate(req.workdir), stem=f"{req.stem()}-pages"))
    return ConversionOutcome(
        path=archive,
        name=f"{req.stem()}-pages.zip",
        mime="application/zip",
        extra_notes=[f"共 {len(images)} 页，已打包为 ZIP。"],
    )


def _pdf_to_docx(req: ConversionRequest) -> ConversionOutcome:
    """PDF → DOCX（pdf2docx，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    output = req.workdir / "output.docx"
    pdf_to_docx(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("docx"), mime=_mime_for("docx"))


def _pdf_to_xlsx(req: ConversionRequest) -> ConversionOutcome:
    """PDF → XLSX（pdfplumber 表格识别，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    output = req.workdir / "output.xlsx"
    pdf_to_xlsx(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("xlsx"), mime=_mime_for("xlsx"))


def _pdf_to_pptx(req: ConversionRequest) -> ConversionOutcome:
    """PDF → PPTX（逐页光栅化，C 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    output = req.workdir / "output.pptx"
    pdf_to_pptx(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(
        path=_stage(output),
        name=req.output_name("pptx"),
        mime=_mime_for("pptx"),
        extra_notes=["输出为每页一张图片的幻灯片，文字不可编辑（矩阵 C 类）。"],
    )


def _pdf_to_html(req: ConversionRequest) -> ConversionOutcome:
    """PDF → HTML（pdfminer.six，B 类）。

    Args:
        req: 转换请求。

    Returns:
        转换产出。
    """
    output = req.workdir / "output.html"
    pdf_to_html(req.source_path, output, params=req.params, ctx=req.ctx)
    return ConversionOutcome(path=_stage(output), name=req.output_name("html"), mime=_mime_for("html"))


# 分派表：与 matrix.MATRIX 的非空单元一一对应（共 23 条）
_HANDLERS: Final[dict[tuple[str, str], Callable[[ConversionRequest], ConversionOutcome]]] = {
    ("html", "pdf"): _html_to_pdf,
    ("html", "png"): _html_to_png,
    ("html", "docx"): _html_to_docx,
    ("html", "xlsx"): _html_to_xlsx,
    ("docx", "pdf"): _office_to_pdf,
    ("docx", "png"): _office_to_images_zip,
    ("docx", "html"): _docx_to_html,
    ("docx", "xlsx"): _docx_to_xlsx,
    ("docx", "pptx"): _office_to_pptx,
    ("xlsx", "pdf"): _office_to_pdf,
    ("xlsx", "png"): _office_to_images_zip,
    ("xlsx", "html"): _xlsx_to_html,
    ("xlsx", "docx"): _xlsx_to_docx,
    ("pptx", "pdf"): _office_to_pdf,
    ("pptx", "png"): _office_to_images_zip,
    ("pptx", "html"): _pptx_to_html,
    ("pptx", "docx"): _pptx_to_docx,
    ("pptx", "xlsx"): _pptx_to_xlsx,
    ("pdf", "png"): _pdf_to_images_zip,
    ("pdf", "docx"): _pdf_to_docx,
    ("pdf", "xlsx"): _pdf_to_xlsx,
    ("pdf", "pptx"): _pdf_to_pptx,
    ("pdf", "html"): _pdf_to_html,
}


def dispatched_pairs() -> list[tuple[str, str]]:
    """返回所有已实现的分派路径。

    Returns:
        ``(source, target)`` 元组列表。

    Raises:
        AssertionError: 矩阵与分派表不一致（启动自检用，尽早暴露漏实现）。
    """
    from app.modules.converter.matrix import MATRIX

    declared = {
        (source, target) for source, row in MATRIX.items() for target, cell in row.items() if cell.grade
    }
    implemented = set(_HANDLERS)
    missing = declared - implemented
    extra = implemented - declared
    if missing or extra:
        raise AssertionError(f"转换矩阵与实现不一致：未实现 {sorted(missing)}，多余实现 {sorted(extra)}")
    return sorted(implemented)


def run_conversion(request: ConversionRequest) -> ConversionOutcome:
    """执行一次转换。

    Args:
        request: 转换请求。

    Returns:
        转换产出。

    Raises:
        UnsupportedPairError: 组合不受支持（矩阵标记为 ×）。
        EngineUnavailableError: 所需引擎缺失。
        ConversionFailedError: 转换失败。
    """
    pair = (request.source_format, request.target_format)
    if not is_supported(*pair):
        cell = lookup(*pair)
        reason = (cell.note if cell else "") or "该转换方向不在支持范围内"
        raise UnsupportedPairError(
            f"不支持 {request.source_format.upper()} → {request.target_format.upper()} 的转换：{reason}"
        )

    handler = _HANDLERS.get(pair)
    if handler is None:
        # 理论上不可达：dispatched_pairs() 会在启动时校验一致性
        raise UnsupportedPairError(f"转换路径 {pair[0]} → {pair[1]} 尚未实现")

    request.ctx.progress(5)
    started = time.monotonic()
    outcome = handler(request)
    logger.info(
        "转换完成 %s→%s 耗时 %.2fs → %s",
        pair[0],
        pair[1],
        time.monotonic() - started,
        outcome.name,
    )
    return outcome


# ---------------------------------------------------------------- 任务封装


def make_conversion_job(
    *,
    source_path: Path,
    source_format: str,
    target_format: str,
    display_name: str,
    params: ConversionParams,
) -> JobFunc:
    """构造可提交给任务管理器的转换任务函数。

    任务函数负责：
        1. 创建独占临时目录；
        2. 执行转换并把产出搬到暂存区；
        3. 无论成败都回收临时目录与上传文件（NFR-03 禁止磁盘泄漏）。

    Args:
        source_path: 已落盘的源文件。
        source_format: 源格式 id。
        target_format: 目标格式 id。
        display_name: 用户可见的原始文件名。
        params: 转换参数。

    Returns:
        符合 ``JobFunc`` 签名的闭包。
    """

    def job(ctx: JobContext) -> JobResult:
        """执行转换任务。

        Args:
            ctx: 任务上下文。

        Returns:
            任务产出描述。
        """
        workdir = ConversionRequest.create_workdir()
        try:
            request = ConversionRequest(
                source_path=source_path,
                source_format=source_format,
                target_format=target_format,
                params=params,
                display_name=display_name,
                workdir=workdir,
                ctx=ctx,
            )
            outcome = run_conversion(request)
            ctx.progress(95)
            # 兜底：处理器若忘了把产出移出 workdir，这里统一搬运。
            # 少了这一步，下面的 finally 会把产出连同 workdir 一起删掉，
            # 登记阶段就会抛 FileNotFoundError，用户看到的却是"引擎缺失"。
            output_path = _stage(outcome.path) if _inside(outcome.path, workdir) else outcome.path
            return JobResult(path=output_path, name=outcome.name, mime=outcome.mime)
        finally:
            # 顺序很重要：先删任务目录（产出已 stage 出去），再删上传文件
            shutil.rmtree(workdir, ignore_errors=True)
            if source_path.exists() and source_path.parent == upload_store.root:
                source_path.unlink(missing_ok=True)

    return job


def submit_conversion(
    *,
    source_path: Path,
    source_format: str,
    target_format: str,
    display_name: str,
    params: ConversionParams,
    file_size: int,
    sha256_prefix: str,
) -> dict[str, object]:
    """提交一个转换任务。

    Args:
        source_path: 已落盘的源文件。
        source_format: 源格式 id。
        target_format: 目标格式 id。
        display_name: 用户可见的原始文件名。
        params: 转换参数。
        file_size: 源文件字节数。
        sha256_prefix: 源文件 SHA256 前 8 位。

    Returns:
        任务的 API 表示（前端直接消费）。
    """
    manager = get_task_manager()
    record = manager.submit(
        module="converter",
        operation=f"{source_format}2{target_format}",
        inputs=[TaskInput(name=display_name, size=file_size, sha256=sha256_prefix)],
        job=make_conversion_job(
            source_path=source_path,
            source_format=source_format,
            target_format=target_format,
            display_name=display_name,
            params=params,
        ),
    )
    return record.to_api()


def registry_ttl_hours() -> int:
    """取产物保留时长（小时）。

    Returns:
        配置中的 ``limits.output_ttl_hours``。
    """
    return get_settings().limits.output_ttl_hours


# 供路由层复用的输出登记类型别名（避免路由直接 import storage 内部类型）
RegisteredOutput = OutputFile

__all__ = [
    "ConversionOutcome",
    "dispatched_pairs",
    "make_conversion_job",
    "registry_ttl_hours",
    "run_conversion",
    "submit_conversion",
]
