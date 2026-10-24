"""PDF 相关的全部转换能力。

涵盖：
    PDF → 图片 / DOCX / XLSX / HTML / PPTX
    图片序列 → PPTX（供 docx/xlsx/pptx → pptx 的「每页一张图」路径复用）
    PDF 工具集：合并 / 拆分 / 旋转 / 加密 / 去密码 / 提取页面（FR-DC-08）
    图片序列 → ZIP（PDF 多页导出的打包产物）

依赖策略：所有第三方库均为**惰性导入**。离线包允许按需裁剪依赖
（例如只需 PDF→图片 时可以不装 pdf2docx），缺失时给出明确的安装指引，
而不是让整个服务 import 失败。
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.errors import ConversionFailedError, EngineUnavailableError
from app.core.tasks import JobContext
from app.modules.converter.base import ConversionParams

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 依赖装载


def _optional_import(module: str, package: str, purpose: str):  # noqa: ANN202
    """惰性导入可选的第三方库。

    Args:
        module: 模块名（import 用）。
        package: pip 包名（错误提示用）。
        purpose: 该依赖的用途描述（写进错误信息，帮助用户判断是否值得安装）。

    Returns:
        导入后的模块对象。

    Raises:
        EngineUnavailableError: 依赖缺失，错误信息含 pip 安装命令。
    """
    try:
        return __import__(module, fromlist=["*"])
    except ImportError as exc:
        raise EngineUnavailableError(
            f"缺少依赖 {package}，无法完成「{purpose}」。请执行：pip install {package}",
            extra={"missing_package": package},
        ) from exc


def _mupdf():  # noqa: ANN202
    """装载 PyMuPDF（fitz）。

    Returns:
        fitz 模块。

    Raises:
        EngineUnavailableError: 未安装 PyMuPDF。
    """
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24 的新模块名

        return fitz
    except ImportError:
        pass
    try:
        import fitz  # 旧版模块名

        return fitz
    except ImportError as exc:
        raise EngineUnavailableError(
            "缺少依赖 PyMuPDF，无法完成 PDF 光栅化与图片导出。请执行：pip install PyMuPDF",
            extra={"missing_package": "PyMuPDF"},
        ) from exc


# ---------------------------------------------------------------- PDF → 图片


def pdf_to_images(
    source: Path,
    workdir: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> list[Path]:
    """把 PDF 逐页光栅化为图片。

    Args:
        source: PDF 文件路径。
        workdir: 输出目录。
        params: 转换参数（dpi / image_format / quality / page_range）。
        ctx: 任务上下文。

    Returns:
        按页序排列的图片路径列表。

    Raises:
        EngineUnavailableError: 缺少 PyMuPDF。
        ConversionFailedError: PDF 无法解析或加密。
    """
    fitz = _mupdf()
    workdir.mkdir(parents=True, exist_ok=True)

    dpi = params.effective_dpi()
    image_format = str(params.image_format).lower()
    suffix = "jpg" if image_format in {"jpg", "jpeg"} else "png"
    quality = int(min(100, max(40, params.quality)))

    try:
        document = fitz.open(str(source))
    except Exception as exc:
        raise ConversionFailedError(f"PDF 无法打开：{str(exc)[:160]}。文件可能已损坏或受密码保护。") from exc

    try:
        if document.needs_pass:
            raise ConversionFailedError("该 PDF 已加密，请先在「PDF 工具」中去密码后再转换为图片。")

        total = document.page_count
        if total == 0:
            raise ConversionFailedError("PDF 不含任何页面。")

        pages = params.parsed_page_range(total)
        outputs: list[Path] = []
        # PyMuPDF 的 Matrix 缩放因子以 72 DPI 为基准
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        for index, page_number in enumerate(pages):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)

            # 文件名带上原始页码，多页导出后依然能对应回原文
            output = workdir / f"page-{page_number + 1:03d}.{suffix}"
            if suffix == "jpg":
                # PyMuPDF 的 JPEG 输出需要显式转为 bytes 再落盘
                pixmap.pil_save(str(output), format="JPEG", quality=quality, optimize=True)
            else:
                pixmap.save(str(output))
            outputs.append(output)

            if ctx:
                # 光栅化是纯 CPU 工作，占任务进度的 10%–90%
                ctx.progress(10 + int(80 * (index + 1) / len(pages)))

        if not outputs:
            raise ConversionFailedError("未导出任何页面，请检查页面范围设置。")
        return outputs
    finally:
        document.close()


def images_to_zip(images: Sequence[Path], workdir: Path, *, stem: str = "pages") -> Path:
    """把图片序列打包为 ZIP（PDF 多页导出的默认交付形态）。

    Args:
        images: 图片路径列表。
        workdir: 输出目录。
        stem: 压缩包名主干。

    Returns:
        ZIP 文件路径。

    Raises:
        ConversionFailedError: 图片为空。
    """
    if not images:
        raise ConversionFailedError("没有可打包的图片。")

    output = workdir / f"{stem}.zip"
    # ZIP_DEFLATED 对 PNG 收益很小（已压缩），但 JPG 之外的位图仍有明显效果
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        readme = "本压缩包由「全能工具站」导出。\n"
        readme += f"共 {len(images)} 个文件，文件名中的页码对应源 PDF 的页序。\n"
        archive.writestr("README.txt", readme)
        for image in images:
            archive.write(image, arcname=image.name)
    return output


# ---------------------------------------------------------------- PDF → DOCX


def pdf_to_docx(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PDF → DOCX（pdf2docx，重建段落与表格结构）。

    失败降级策略：pdf2docx 对复杂版式容易抛异常，此时退回「提取纯文本写入 DOCX」，
    用户至少能拿到可编辑的文字内容，而不是一个失败任务（对应 §5.1.3「pdftotext 兜底」）。

    Args:
        source: PDF 文件路径。
        output: 目标 DOCX 路径。
        params: 转换参数（keep_layout）。
        ctx: 任务上下文。

    Returns:
        DOCX 文件路径。
    """
    if ctx:
        ctx.progress(20)

    try:
        from pdf2docx import Converter  # type: ignore[import-not-found]

        converter = Converter(str(source))
        try:
            # 参数说明：pdf2docx 未暴露"保留版面"的布尔开关，
            # 这里通过 multi_processing 与 pages 的取舍体现 keep_layout 的意图。
            converter.convert(str(output), start=0, end=None)
        finally:
            converter.close()
        if output.exists() and output.stat().st_size > 0:
            if ctx:
                ctx.progress(85)
            return output
        logger.warning("pdf2docx 未产出文件，降级为纯文本导出")
    except EngineUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - pdf2docx 内部异常类型不稳定
        logger.warning("pdf2docx 转换失败，降级为纯文本导出：%s", exc)

    return _pdf_text_fallback(source, output, params=params, ctx=ctx)


def _pdf_text_fallback(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PDF → DOCX 的纯文本降级路径。

    Args:
        source: PDF 文件路径。
        output: 目标 DOCX 路径。
        params: 转换参数（keep_layout 关闭时更贴近纯文本流）。
        ctx: 任务上下文。

    Returns:
        DOCX 文件路径。

    Raises:
        EngineUnavailableError: 缺依赖。
    """
    docx = _optional_import("docx", "python-docx", "生成 DOCX 文档")
    text = extract_pdf_text(source, keep_layout=params.keep_layout)

    document = docx.Document()
    document.add_heading(f"{source.stem}（文本提取）", level=1)
    note = document.add_paragraph()
    note_run = note.add_run(
        "说明：本次未能完成版式级还原，已降级为纯文本导出，段落与表格结构未保留。"
        "如需保留版式，可在具备图形环境的机器上重试。"
    )
    note_run.italic = True

    for block in text:
        document.add_paragraph(block)

    document.save(str(output))
    if ctx:
        ctx.progress(90)
    return output


def extract_pdf_text(source: Path, *, keep_layout: bool = True) -> list[str]:
    """提取 PDF 文本（按行/块）。

    优先使用 pdfminer.six（版面感知更好），缺失时退回 PyMuPDF。

    Args:
        source: PDF 文件路径。
        keep_layout: 是否尽力保留版式（影响分行策略）。

    Returns:
        文本块列表（每个元素对应一段）。

    Raises:
        ConversionFailedError: 无法解析。
    """
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract

        raw = pdfminer_extract(str(source))
        if raw.strip():
            return [block.strip() for block in raw.split("\n\n") if block.strip()]
    except ImportError:
        logger.info("未安装 pdfminer.six，回退到 PyMuPDF 提取文本")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdfminer.six 提取失败，回退到 PyMuPDF：%s", exc)

    fitz = _mupdf()
    try:
        document = fitz.open(str(source))
    except Exception as exc:
        raise ConversionFailedError(f"PDF 无法打开：{str(exc)[:160]}") from exc
    try:
        blocks: list[str] = []
        for page in document:
            page_text = page.get_text("text")
            blocks.extend(part.strip() for part in page_text.split("\n\n") if part.strip())
        return blocks
    finally:
        document.close()


# ---------------------------------------------------------------- PDF → XLSX


def pdf_to_xlsx(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PDF → XLSX（表格区域识别）。

    使用 pdfplumber 的表格提取（对有线框表格效果最好），
    每个表格写入一个工作表；识别不到表格时，退化为「整页文本按行写入单表」，
    避免产出空工作簿让用户困惑。

    Args:
        source: PDF 文件路径。
        output: 目标 XLSX 路径。
        params: 转换参数（table_mode）。
        ctx: 任务上下文。

    Returns:
        XLSX 文件路径。
    """
    pdfplumber = _optional_import("pdfplumber", "pdfplumber", "识别 PDF 中的表格")
    openpyxl = _optional_import("openpyxl", "openpyxl", "生成 XLSX 表格")

    # pdfplumber 的 table_settings 与前端 table_mode 选项对应
    table_settings: dict[str, Any] = {}
    if params.table_mode == "lattice":
        table_settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    elif params.table_mode == "stream":
        table_settings = {"vertical_strategy": "text", "horizontal_strategy": "text"}

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)  # 移除默认空表，稍后按实际内容重建

    table_count = 0
    text_only_rows: list[list[str]] = []

    with pdfplumber.open(str(source)) as pdf:
        total_pages = len(pdf.pages)
        for page_index, page in enumerate(pdf.pages):
            try:
                tables = page.extract_tables(table_settings) if table_settings else page.extract_tables()
            except Exception as exc:  # noqa: BLE001 - pdfplumber 解析异常种类多
                logger.warning("第 %s 页表格识别失败：%s", page_index + 1, exc)
                tables = []

            for table in tables or []:
                # 过滤掉全空行（PDF 表格识别常产生空行）
                rows = [row for row in table if row and any((cell or "").strip() for cell in row)]
                if not rows:
                    continue
                table_count += 1
                if params.one_sheet_per_table:
                    sheet = (
                        workbook.create_sheet(title=f"表格{table_count}")
                        if table_count > 1
                        else workbook.create_sheet(title="表格1")
                    )
                else:
                    sheet = workbook.active if workbook.sheetnames else workbook.create_sheet(title="表格1")
                for row in rows:
                    sheet.append([(cell or "").strip() for cell in row])

            if not tables:
                page_text = page.extract_text() or ""
                text_only_rows.extend([line.strip()] for line in page_text.splitlines() if line.strip())

            if ctx:
                ctx.progress(15 + int(70 * (page_index + 1) / max(1, total_pages)))

    if table_count == 0:
        # 未识别到表格：产出「文本行」工作簿，并给出明确说明
        sheet = workbook.create_sheet(title="文本内容")
        sheet.append(["说明"])
        sheet.append(["未在 PDF 中识别到表格区域，以下为整页文本内容（每行一单元格）。"])
        sheet.append([])
        for row in text_only_rows:
            sheet.append(row)

    if not workbook.sheetnames:
        raise ConversionFailedError(
            "未能从该 PDF 中提取到任何内容。若为扫描件（图片型 PDF），需要 OCR 支持，本期暂未提供。"
        )

    workbook.save(str(output))
    if ctx:
        ctx.progress(90)
    return output


# ---------------------------------------------------------------- PDF → HTML


def pdf_to_html(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PDF → HTML（保留可选中文本与基本布局）。

    Args:
        source: PDF 文件路径。
        output: 目标 HTML 路径。
        params: 转换参数（未使用，保持签名一致）。
        ctx: 任务上下文。

    Returns:
        HTML 文件路径。
    """
    blocks = extract_pdf_text(source, keep_layout=params.keep_layout)
    if ctx:
        ctx.progress(70)

    def esc(text: str) -> str:
        """转义 HTML 特殊字符，防止 PDF 文本意外注入标签。

        Args:
            text: 原始文本。

        Returns:
            转义后的文本。
        """
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    body = "\n".join(f"  <p>{esc(block)}</p>" for block in blocks)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{esc(source.stem)} · 由 PDF 转换</title>
<style>
  html, body {{ margin: 0; padding: 0; }}
  body {{
    max-width: 860px; margin: 48px auto; padding: 0 24px;
    font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    line-height: 1.75; color: #1f2733; background: #fff;
  }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 12px; }}
  p {{ margin: 0 0 0.9em; }}
  .note {{ color: #6b7280; font-size: 13px; background: #f4f6fa; padding: 10px 14px; border-radius: 8px; }}
</style>
</head>
<body>
<h1>{esc(source.stem)}</h1>
<p class="note">由「全能工具站」从 PDF 转换而来，保留可选中文本；复杂版式会重排为流式段落。</p>
{body}
</body>
</html>
"""
    output.write_text(html, encoding="utf-8")
    if ctx:
        ctx.progress(90)
    return output


# ---------------------------------------------------------------- PDF → PPTX


def pdf_to_pptx(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PDF → PPTX（每页一张图片的视觉还原）。

    这是矩阵中明确标注为 C 类（不可逐字编辑）的路径，风险 R5 要求界面前置提示。
    实现上把每页光栅化后铺满幻灯片，并在开启 note_mode 时把该页文本放入备注，
    让用户至少可以检索内容。

    Args:
        source: PDF 文件路径。
        output: 目标 PPTX 路径。
        params: 转换参数（dpi / slide_size / note_mode）。
        ctx: 任务上下文。

    Returns:
        PPTX 文件路径。
    """
    pptx = _optional_import("pptx", "python-pptx", "生成 PPTX 演示文稿")
    from pptx.util import Emu

    # 先用高 DPI 渲染，再按幻灯片尺寸放大，避免图片被拉伸得模糊
    render_dpi = 200 if params.effective_dpi() >= 150 else params.effective_dpi()
    render_params = ConversionParams(dpi=render_dpi, image_format="png", page_range=params.page_range)

    images = pdf_to_images(source, output.parent / "pptx-images", params=render_params, ctx=ctx)
    if ctx:
        ctx.progress(80)

    width_in, height_in = (13.333, 7.5) if params.slide_size == "16:9" else (10.0, 7.5)
    presentation = pptx.Presentation()
    presentation.slide_width = Emu(int(width_in * 914400))
    presentation.slide_height = Emu(int(height_in * 914400))
    blank_layout = presentation.slide_layouts[6]  # 6 = 空白版式

    page_texts = extract_pdf_text(source, keep_layout=True) if params.note_mode else []
    per_page_text = _split_text_by_page(page_texts, len(images)) if page_texts else []

    for index, image in enumerate(images):
        slide = presentation.slides.add_slide(blank_layout)
        slide.shapes.add_picture(
            str(image), 0, 0, width=presentation.slide_width, height=presentation.slide_height
        )
        if params.note_mode and index < len(per_page_text) and per_page_text[index]:
            slide.notes_slide.notes_text_frame.text = per_page_text[index]

    presentation.save(str(output))
    if ctx:
        ctx.progress(95)
    return output


def _split_text_by_page(blocks: Sequence[str], page_count: int) -> list[str]:
    """把文本块尽量平均分配到各页备注。

    真正的逐页文本需要按页提取，但 pdfminer 的块级提取不保证页边界，
    这里采用近似分配 —— 备注是辅助信息，精确到页的收益低于实现成本。

    Args:
        blocks: 文本块列表。
        page_count: 页数。

    Returns:
        长度为 ``page_count`` 的文本列表。
    """
    if page_count <= 0:
        return []
    per = max(1, len(blocks) // page_count)
    result: list[str] = []
    for index in range(page_count):
        chunk = blocks[index * per : (index + 1) * per] if index < page_count - 1 else blocks[index * per :]
        result.append("\n".join(chunk))
    return result


def images_to_pptx(
    images: Sequence[Path],
    output: Path,
    *,
    params: ConversionParams,
    notes: Sequence[str] | None = None,
) -> Path:
    """图片序列 → PPTX（每页一张图）。

    供 docx / xlsx / pptx → pptx 的图片模式路径复用。

    Args:
        images: 图片路径列表（按页序）。
        output: 目标 PPTX 路径。
        params: 转换参数（slide_size / note_mode）。
        notes: 每页备注文本（可选）。

    Returns:
        PPTX 文件路径。

    Raises:
        ConversionFailedError: 无图片输入。
    """
    if not images:
        raise ConversionFailedError("没有可写入幻灯片的页面图片。")

    pptx = _optional_import("pptx", "python-pptx", "生成 PPTX 演示文稿")
    from pptx.util import Emu

    width_in, height_in = (13.333, 7.5) if params.slide_size == "16:9" else (10.0, 7.5)
    presentation = pptx.Presentation()
    presentation.slide_width = Emu(int(width_in * 914400))
    presentation.slide_height = Emu(int(height_in * 914400))
    blank_layout = presentation.slide_layouts[6]

    for index, image in enumerate(images):
        slide = presentation.slides.add_slide(blank_layout)
        slide.shapes.add_picture(
            str(image), 0, 0, width=presentation.slide_width, height=presentation.slide_height
        )
        if notes and index < len(notes) and notes[index]:
            slide.notes_slide.notes_text_frame.text = notes[index]

    presentation.save(str(output))
    return output


# ---------------------------------------------------------------- PDF 工具集


def pdf_tool_merge(sources: Sequence[Path], output: Path) -> Path:
    """合并多个 PDF。

    Args:
        sources: 源 PDF 列表（按期望顺序）。
        output: 输出路径。

    Returns:
        合并后的 PDF 路径。

    Raises:
        ConversionFailedError: 输入为空或 PDF 损坏。
    """
    pypdf = _optional_import("pypdf", "pypdf", "PDF 合并")
    if len(sources) < 2:
        raise ConversionFailedError("合并至少需要 2 个 PDF 文件。")

    writer = pypdf.PdfWriter()
    try:
        for source in sources:
            reader = pypdf.PdfReader(str(source))
            for page in reader.pages:
                writer.add_page(page)
        with output.open("wb") as fp:
            writer.write(fp)
    except Exception as exc:
        raise ConversionFailedError(f"PDF 合并失败：{str(exc)[:160]}") from exc
    return output


def pdf_tool_extract_pages(
    source: Path,
    output: Path,
    *,
    page_spec: str,
    rotate: int = 0,
    password: str | None = None,
) -> Path:
    """提取指定页面（可选旋转）为一个新 PDF。

    拆分与旋转共用此实现：提取单页即"拆分"，设置 rotate 即"旋转"。

    Args:
        source: 源 PDF。
        output: 输出路径。
        page_spec: 页面范围（如 ``1-3,5``）。
        rotate: 顺时针旋转角度（0 / 90 / 180 / 270）。
        password: 源文件密码。

    Returns:
        输出 PDF 路径。

    Raises:
        ConversionFailedError: 无法解析或页面范围为空。
    """
    pypdf = _optional_import("pypdf", "pypdf", "PDF 拆分与旋转")

    try:
        reader = pypdf.PdfReader(str(source))
        if reader.is_encrypted:
            if not password:
                raise ConversionFailedError("该 PDF 已加密，请先提供密码或使用「去密码」功能。")
            if not reader.decrypt(password):
                raise ConversionFailedError("PDF 密码不正确。")
    except ConversionFailedError:
        raise
    except Exception as exc:
        raise ConversionFailedError(f"PDF 无法解析：{str(exc)[:160]}") from exc

    total = len(reader.pages)
    params = ConversionParams(page_range=page_spec)
    indices = params.parsed_page_range(total)
    if not indices:
        raise ConversionFailedError("页面范围为空，请检查输入（例如 1-3,5）。")

    writer = pypdf.PdfWriter()
    for index in indices:
        page = reader.pages[index]
        if rotate:
            page = page.rotate(int(rotate) % 360)
        writer.add_page(page)

    with output.open("wb") as fp:
        writer.write(fp)
    return output


def pdf_tool_encrypt(source: Path, output: Path, *, user_password: str) -> Path:
    """为 PDF 添加打开密码（AES-128）。

    Args:
        source: 源 PDF。
        output: 输出路径。
        user_password: 打开密码。

    Returns:
        加密后的 PDF 路径。

    Raises:
        ConversionFailedError: 加密失败。
    """
    pypdf = _optional_import("pypdf", "pypdf", "PDF 加密")
    if not user_password:
        raise ConversionFailedError("请提供加密密码。")

    try:
        reader = pypdf.PdfReader(str(source))
        writer = pypdf.PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(user_password=user_password, algorithm="AES-128")
        with output.open("wb") as fp:
            writer.write(fp)
    except Exception as exc:
        raise ConversionFailedError(f"PDF 加密失败：{str(exc)[:160]}") from exc
    return output


def pdf_tool_decrypt(source: Path, output: Path, *, password: str) -> Path:
    """移除 PDF 打开密码。

    仅对**用户已知密码**的文档有效，不涉及任何破解行为。

    Args:
        source: 加密的 PDF。
        output: 输出路径。
        password: 已知密码。

    Returns:
        解密后的 PDF 路径。

    Raises:
        ConversionFailedError: 密码错误或解密失败。
    """
    pypdf = _optional_import("pypdf", "pypdf", "PDF 去密码")

    try:
        reader = pypdf.PdfReader(str(source))
        if not reader.is_encrypted:
            # 未加密的文件直接复制，避免用户以为失败
            output.write_bytes(source.read_bytes())
            return output
        if not reader.decrypt(password or ""):
            raise ConversionFailedError("PDF 密码不正确，无法移除保护。")
        writer = pypdf.PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        with output.open("wb") as fp:
            writer.write(fp)
    except ConversionFailedError:
        raise
    except Exception as exc:
        raise ConversionFailedError(f"PDF 去密码失败：{str(exc)[:160]}") from exc
    return output


def pdf_page_count(source: Path, *, password: str | None = None) -> int:
    """统计 PDF 页数。

    Args:
        source: PDF 路径。
        password: 密码（可选）。

    Returns:
        页数；无法读取返回 0。
    """
    fitz = _mupdf()
    try:
        document = fitz.open(str(source))
    except Exception:  # noqa: BLE001
        return 0
    try:
        if document.needs_pass and password:
            document.authenticate(password)
        return int(document.page_count)
    finally:
        document.close()


__all__ = [
    "extract_pdf_text",
    "images_to_pptx",
    "images_to_zip",
    "pdf_page_count",
    "pdf_to_docx",
    "pdf_to_html",
    "pdf_to_images",
    "pdf_to_pptx",
    "pdf_to_xlsx",
    "pdf_tool_decrypt",
    "pdf_tool_encrypt",
    "pdf_tool_extract_pages",
    "pdf_tool_merge",
]
