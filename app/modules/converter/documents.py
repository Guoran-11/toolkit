"""Office 文档之间的结构性转换（不经过 PDF 中转的那些路径）。

与 ``office.py`` 的分工：
    office.py      —— 交给 LibreOffice 的「保真」路径（多为 A 类）
    documents.py   —— 用 Python 库做「结构提取」的路径（多为 B 类）

这里刻意不使用 LibreOffice 处理 DOCX → HTML：
LO 导出的 HTML 会带数百行内联样式与 Office 命名空间声明，可读性极差，
而 mammoth 产出的是语义化标签（``<h1>``/``<p>``/``<table>``），
更适合用户拿去二次利用 —— 这正是前端矩阵标注 "A 类：结构清晰、语义化" 的依据。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.errors import ConversionFailedError
from app.core.tasks import JobContext
from app.modules.converter.base import ConversionParams
from app.modules.converter.pdf import _optional_import

logger = logging.getLogger(__name__)


def _html_escape(text: str) -> str:
    """转义 HTML 特殊字符。

    Args:
        text: 原始文本。

    Returns:
        转义后的安全文本。
    """
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _wrap_html_document(title: str, body: str, *, note: str = "") -> str:
    """把片段包装成完整的、自带样式的 HTML 文档。

    Args:
        title: 文档标题。
        body: 正文 HTML 片段。
        note: 顶部说明文案。

    Returns:
        完整 HTML 文本。
    """
    note_html = f'<p class="note">{_html_escape(note)}</p>' if note else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{_html_escape(title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; }}
  body {{
    max-width: 900px; margin: 48px auto; padding: 0 24px;
    font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    line-height: 1.75; color: #1f2733; background: #fff;
  }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 12px; }}
  h2 {{ font-size: 18px; margin-top: 2em; }}
  h3 {{ font-size: 16px; margin-top: 1.6em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: 14px; }}
  th, td {{ border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f3f4f6; font-weight: 600; }}
  img {{ max-width: 100%; height: auto; }}
  .note {{ color: #6b7280; font-size: 13px; background: #f4f6fa; padding: 10px 14px; border-radius: 8px; }}
  pre {{ background: #f4f6fa; padding: 14px; border-radius: 8px; overflow: auto; }}
  blockquote {{ margin: 1em 0; padding: 0.4em 1em; border-left: 3px solid #2563eb; color: #4b5563; }}
</style>
</head>
<body>
<h1>{_html_escape(title)}</h1>
{note_html}
{body}
</body>
</html>
"""


# ---------------------------------------------------------------- DOCX → HTML


def docx_to_html(source: Path, output: Path, *, ctx: JobContext | None = None) -> Path:
    """DOCX → HTML（mammoth，语义化输出）。

    Args:
        source: DOCX 文件路径。
        output: 目标 HTML 路径。
        ctx: 任务上下文。

    Returns:
        HTML 文件路径。
    """
    mammoth = _optional_import("mammoth", "mammoth", "将 DOCX 转为语义化 HTML")
    if ctx:
        ctx.progress(30)

    try:
        with source.open("rb") as fp:
            result = mammoth.convert_to_html(fp)
    except Exception as exc:
        raise ConversionFailedError(
            f"DOCX 解析失败：{str(exc)[:160]}。文件可能已损坏或为 .doc 旧格式（请先另存为 .docx）。"
        ) from exc

    warnings = result.messages or []
    note = "由 DOCX 转换而来，已简化为语义化标签。"
    if warnings:
        # 只提示数量与首条，避免把 mammoth 的英文堆栈塞给用户
        first = str(warnings[0].message if hasattr(warnings[0], "message") else warnings[0])
        note += f" 转换过程中有 {len(warnings)} 处样式被简化（{first[:80]}）。"

    html = _wrap_html_document(source.stem, result.value, note=note)
    output.write_text(html, encoding="utf-8")
    if ctx:
        ctx.progress(85)
    return output


# ---------------------------------------------------------------- 表格提取


def _write_tables_to_xlsx(
    tables: Sequence[Sequence[Sequence[str]]],
    output: Path,
    *,
    one_sheet_per_table: bool,
    source_label: str,
    fallback_rows: Sequence[Sequence[str]] | None = None,
    fallback_sheet_title: str = "正文",
) -> Path:
    """把表格数据写入 XLSX；一个表格都没有时退化为导出正文文本。

    为什么要有 ``fallback_rows``：Word 里绝大多数文档（教案、答案、报告）是
    纯段落结构，按老实现会直接抛"未识别到表格"而失败 —— 用户看到的是一次
    转换失败，而不是"这份文档没有表格"。此处改为把正文段落导出到独立工作表，
    保证「Word → XLSX」在文档有文本时**永远有产出**。

    Args:
        tables: 三维列表 —— [表][行][单元格]。
        output: 目标 XLSX 路径。
        one_sheet_per_table: 是否每个表格独立工作表。
        source_label: 源描述（写入说明行）。
        fallback_rows: 无表格（或需要并排导出）时的正文行 —— [行][单元格]。
        fallback_sheet_title: 正文工作表名。

    Returns:
        XLSX 文件路径。

    Raises:
        ConversionFailedError: 表格与正文均为空，确实没有任何可导出内容。
    """
    openpyxl = _optional_import("openpyxl", "openpyxl", "生成 XLSX 表格")
    valid = [
        [row for row in table if row and any((str(cell or "").strip()) for cell in row)] for table in tables
    ]
    valid = [table for table in valid if table]

    text_rows = [
        [str(cell) for cell in row]
        for row in (fallback_rows or [])
        if row and any(str(cell or "").strip() for cell in row)
    ]

    if not valid and not text_rows:
        raise ConversionFailedError(
            f"未在{source_label}中识别到可导出的内容：既没有表格，也没有可提取的正文。"
            "若这是扫描件或纯图片文档，请改用「转 PDF」保留原始版面。"
        )

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    if valid:
        if one_sheet_per_table:
            for index, table in enumerate(valid, start=1):
                sheet = workbook.create_sheet(title=f"表格{index}")
                for row in table:
                    sheet.append([str(cell) if cell is not None else "" for cell in row])
        else:
            sheet = workbook.create_sheet(title="表格")
            for table in valid:
                for row in table:
                    sheet.append([str(cell) if cell is not None else "" for cell in row])
                sheet.append([])  # 表格之间留空行分隔

    if text_rows:
        # 正文单独成表：与表格混在一张表里会让列宽与表头关系错乱
        sheet = workbook.create_sheet(title=fallback_sheet_title)
        for row in text_rows:
            sheet.append(row)

    workbook.save(str(output))
    return output


def docx_to_xlsx(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """DOCX → XLSX（表格提取 + 正文段落）。

    Args:
        source: DOCX 文件路径。
        output: 目标 XLSX 路径。
        params: 转换参数（one_sheet_per_table）。
        ctx: 任务上下文。

    Returns:
        XLSX 文件路径。
    """
    docx = _optional_import("docx", "python-docx", "读取 DOCX 中的表格")
    try:
        document = docx.Document(str(source))
    except Exception as exc:
        raise ConversionFailedError(f"DOCX 无法打开：{str(exc)[:160]}") from exc

    if ctx:
        ctx.progress(40)

    tables = [[[cell.text for cell in row.cells] for row in table.rows] for table in document.tables]
    # 表格单元格的段落在 document.paragraphs 里不会出现，因此不存在重复导出
    paragraphs = [
        [text] for text in ((paragraph.text or "").strip() for paragraph in document.paragraphs) if text
    ]
    return _write_tables_to_xlsx(
        tables,
        output,
        one_sheet_per_table=params.one_sheet_per_table,
        source_label="文档",
        fallback_rows=paragraphs,
        fallback_sheet_title="正文",
    )


def pptx_to_xlsx(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PPTX → XLSX（表格提取 + 幻灯片文本）。

    Args:
        source: PPTX 文件路径。
        output: 目标 XLSX 路径。
        params: 转换参数（one_sheet_per_table）。
        ctx: 任务上下文。

    Returns:
        XLSX 文件路径。
    """
    pptx = _optional_import("pptx", "python-pptx", "读取 PPTX 中的表格")
    try:
        presentation = pptx.Presentation(str(source))
    except Exception as exc:
        raise ConversionFailedError(f"PPTX 无法打开：{str(exc)[:160]}") from exc

    if ctx:
        ctx.progress(40)

    tables: list[list[list[str]]] = []
    slide_texts: list[list[str]] = []
    for index, slide in enumerate(presentation.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                table = shape.table
                tables.append(
                    [[table.cell(r, c).text for c in range(len(table.columns))] for r in range(len(table.rows))]
                )
                continue  # 表格文本已单独提取，不在文本表里重复
            if getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    lines.append(text)
        if lines:
            slide_texts.append([f"第 {index} 页", "\n".join(lines)])

    return _write_tables_to_xlsx(
        tables,
        output,
        one_sheet_per_table=params.one_sheet_per_table,
        source_label="演示文稿",
        fallback_rows=slide_texts,
        fallback_sheet_title="幻灯片文本",
    )


# ---------------------------------------------------------------- PPTX → DOCX


def pptx_to_docx(
    source: Path,
    output: Path,
    workdir: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """PPTX → DOCX（备注文字 + 每页缩略图）。

    矩阵标注为 B 类「备注 + 图片，版式不保留」。实现路径：
        1. 用 LibreOffice 把 PPTX 转 PDF，再光栅化得到每页缩略图；
        2. 用 python-docx 逐页插入「第 N 页」标题 + 缩略图 + 备注文本。

    备注文本从原 PPTX 直接读取（比从 PDF 反推更准确）。

    Args:
        source: PPTX 文件路径。
        output: 目标 DOCX 路径。
        workdir: 任务临时目录（用于 PDF 中转与图片）。
        params: 转换参数。
        ctx: 任务上下文。

    Returns:
        DOCX 文件路径。
    """
    docx = _optional_import("docx", "python-docx", "生成 DOCX 文档")
    pptx = _optional_import("pptx", "python-pptx", "读取 PPTX 备注")

    from app.modules.converter.office import office_to_pdf
    from app.modules.converter.pdf import pdf_to_images

    # 1. 取备注
    try:
        presentation = pptx.Presentation(str(source))
        notes: list[str] = []
        for slide in presentation.slides:
            text = ""
            if slide.has_notes_slide:
                text = (slide.notes_slide.notes_text_frame.text or "").strip()
            notes.append(text)
    except Exception as exc:
        raise ConversionFailedError(f"PPTX 无法打开：{str(exc)[:160]}") from exc

    if ctx:
        ctx.progress(15)

    # 2. 经 PDF 取缩略图
    pdf_path = office_to_pdf(source, workdir, ctx)
    images = pdf_to_images(
        pdf_path,
        workdir / "thumbs",
        params=ConversionParams(dpi=96, image_format="png", page_range="all"),
        ctx=ctx,
    )

    if ctx:
        ctx.progress(75)

    # 3. 组装 DOCX
    document = docx.Document()
    document.add_heading(f"{source.stem}（幻灯片备注与缩略图）", level=1)
    intro = document.add_paragraph()
    intro_run = intro.add_run(
        "说明：PPTX 的版面无法直接映射为 Word 段落结构，因此本文件按「每页缩略图 + 备注文字」组织，"
        "版式不保留。如需保留版式，请转换为 PDF。"
    )
    intro_run.italic = True

    from docx.shared import Inches

    for index, image in enumerate(images):
        document.add_heading(f"第 {index + 1} 页", level=2)
        try:
            # 宽度限制为 6 英寸，适配 A4 页宽且留出页边距
            document.add_picture(str(image), width=Inches(6))
        except Exception as exc:  # noqa: BLE001
            logger.warning("插入第 %s 页缩略图失败：%s", index + 1, exc)
        if index < len(notes) and notes[index]:
            note_paragraph = document.add_paragraph()
            note_paragraph.add_run("备注：").bold = True
            note_paragraph.add_run(notes[index])

    document.save(str(output))
    return output


# ---------------------------------------------------------------- XLSX → HTML


def xlsx_to_html(
    source: Path,
    output: Path,
    *,
    params: ConversionParams,
    ctx: JobContext | None = None,
) -> Path:
    """XLSX → HTML（每个工作表输出为一个表格）。

    Args:
        source: XLSX 文件路径。
        output: 目标 HTML 路径。
        params: 转换参数（inline_css 未参与，样式固定为内嵌 style 块）。
        ctx: 任务上下文。

    Returns:
        HTML 文件路径。
    """
    openpyxl = _optional_import("openpyxl", "openpyxl", "读取 XLSX 工作簿")
    try:
        workbook = openpyxl.load_workbook(str(source), data_only=True)
    except Exception as exc:
        raise ConversionFailedError(f"XLSX 无法打开：{str(exc)[:160]}") from exc

    parts: list[str] = []
    sheet_names = workbook.sheetnames
    for index, name in enumerate(sheet_names):
        sheet = workbook[name]
        rows = list(sheet.iter_rows(values_only=True))
        # 末尾大量空行会让 HTML 中出现无意义的高度，先裁掉
        while rows and all(cell is None or str(cell).strip() == "" for cell in rows[-1]):
            rows.pop()

        parts.append(f"<h2>{_html_escape(name)}</h2>")
        if not rows:
            parts.append("<p>（该工作表为空）</p>")
            continue

        body_rows: list[str] = []
        for row_index, row in enumerate(rows):
            tag = "th" if row_index == 0 else "td"
            cells = "".join(f"<{tag}>{_html_escape('' if cell is None else cell)}</{tag}>" for cell in row)
            body_rows.append(f"<tr>{cells}</tr>")
        parts.append("<table><tbody>" + "".join(body_rows) + "</tbody></table>")

        if ctx:
            ctx.progress(20 + int(65 * (index + 1) / max(1, len(sheet_names))))

    html = _wrap_html_document(
        source.stem,
        "\n".join(parts),
        note=f"由 XLSX 转换而来，共 {len(sheet_names)} 个工作表。公式已按缓存值计算，公式本身不保留。",
    )
    output.write_text(html, encoding="utf-8")
    return output


def xlsx_sheet_preview(source: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    """提取 XLSX 的表格预览数据（供 PDF 工具集之外的内联预览使用）。

    Args:
        source: XLSX 文件路径。
        limit: 每个工作表最多返回的行数。

    Returns:
        ``[{"name": 工作表名, "rows": [[...]]}]``。
    """
    openpyxl = _optional_import("openpyxl", "openpyxl", "读取 XLSX 工作簿")
    workbook = openpyxl.load_workbook(str(source), data_only=True)
    preview: list[dict[str, Any]] = []
    for name in workbook.sheetnames:
        sheet = workbook[name]
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(max_row=limit, values_only=True)
        ]
        preview.append({"name": name, "rows": rows})
    return preview


__all__ = [
    "docx_to_html",
    "docx_to_xlsx",
    "pptx_to_docx",
    "pptx_to_xlsx",
    "xlsx_sheet_preview",
    "xlsx_to_html",
]
