"""把 PRD Markdown 构建为排版好的 DOCX。

支持子集：H1(封面) / H2-H4 标题、段落、无序/有序列表、任务清单、引用、
管道表格、围栏代码块。样式按中文学术/产品文档习惯设置：
标题微软雅黑、正文宋体五号、表格 9pt、代码 Consolas。

用法：
    python docs/build_prd_docx.py <input.md> <output.docx>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# ---------------------------------------------------------------- 常量

NAVY = RGBColor(0x1F, 0x38, 0x64)
BLUE = RGBColor(0x2E, 0x54, 0x96)
GREY = RGBColor(0x59, 0x59, 0x59)
LIGHT_GREY = RGBColor(0x7F, 0x7F, 0x7F)
CODE_RED = RGBColor(0xC7, 0x25, 0x4E)

BODY_FONT = "宋体"
HEAD_FONT = "微软雅黑"
MONO_FONT = "Consolas"

PAGE_W_CM = 21.0
MARGIN_LR_CM = 2.6
CONTENT_W_CM = PAGE_W_CM - MARGIN_LR_CM * 2

# 表格/正文里用到的符号替换，避免缺字形变成豆腐块
SYMBOL_MAP = {
    "✅": "√",
    "❌": "×",
    "⚠️": "△",
    "⚠": "△",
    "✓": "√",
}

INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


# ---------------------------------------------------------------- 底层工具


def set_run_font(run, *, name: str, east_asia: str, size: float, bold: bool = False,
                 color: RGBColor | None = None, italic: bool = False) -> None:
    """设置 run 的三套字体名（ascii/hAnsi/eastAsia）与字号等属性。"""
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), east_asia)
    rfonts.set(qn("w:cs"), name)


def cell_shade(cell, hex_color: str) -> None:
    """单元格底纹。"""
    tcpr = cell._tc.get_or_add_tcPr()
    for old in tcpr.findall(qn("w:shd")):
        tcpr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcpr.append(shd)


def para_shade(paragraph, hex_color: str) -> None:
    """段落底纹。"""
    ppr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    ppr.append(shd)


def para_border(paragraph, *, side: str = "left", color: str = "BFBFBF", size: int = 12) -> None:
    """段落边框（用于引用块左侧竖线）。"""
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    node = OxmlElement(f"w:{side}")
    node.set(qn("w:val"), "single")
    node.set(qn("w:sz"), str(size))
    node.set(qn("w:space"), "8")
    node.set(qn("w:color"), color)
    pbdr.append(node)
    ppr.append(pbdr)


def table_borders(table, color: str = "BFBFBF", size: int = 4) -> None:
    """统一表格边框。"""
    tbl = table._tbl
    tblpr = tbl.tblPr
    for old in tblpr.findall(qn("w:tblBorders")):
        tblpr.remove(old)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), str(size))
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), color)
        borders.append(node)
    tblpr.append(borders)


def table_cell_margins(table, top: int = 40, bottom: int = 40, left: int = 90, right: int = 90) -> None:
    """表格单元格内边距（dxa，1/20 pt）。"""
    tblpr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for name, value in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        node = OxmlElement(f"w:{name}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        mar.append(node)
    tblpr.append(mar)


def set_col_widths(table, widths_cm: list[float]) -> None:
    """同时设置 tblGrid 与每个单元格宽度。

    只设 cell.width 而不改 tblGrid 时，LibreOffice / Word 仍会按等宽列渲染，
    表格看上去和设定完全不符——这是生成 docx 最常见的坑之一。
    """
    table.autofit = False
    for index, width in enumerate(widths_cm):
        if index < len(table.columns):
            table.columns[index].width = Cm(width)
        for row in table.rows:
            if index < len(row.cells):
                row.cells[index].width = Cm(width)


def repeat_header(row) -> None:
    """表头跨页重复。"""
    trpr = row._tr.get_or_add_trPr()
    node = OxmlElement("w:tblHeader")
    node.set(qn("w:val"), "true")
    trpr.append(node)


def no_split(row) -> None:
    """行内禁止分页。"""
    trpr = row._tr.get_or_add_trPr()
    trpr.append(OxmlElement("w:cantSplit"))


def add_field(paragraph, instruction: str) -> None:
    """插入 Word 域（PAGE / NUMPAGES）。"""
    run = paragraph.add_run()
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=8.5, color=LIGHT_GREY)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, sep, text, end):
        run._r.append(node)


def clean(text: str) -> str:
    """符号替换 + 去行内链接。"""
    for src, dst in SYMBOL_MAP.items():
        text = text.replace(src, dst)
    return text


# ---------------------------------------------------------------- 行内排版


def add_inline(paragraph, text: str, *, base_size: float, base_font: str = BODY_FONT,
               color: RGBColor | None = None) -> None:
    """按 **加粗**、`代码`、[文字](链接) 分段写入段落。"""
    text = clean(text)
    for token in INLINE_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            run = paragraph.add_run(token[2:-2])
            set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=base_size,
                         bold=True, color=color or NAVY)
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, name=MONO_FONT, east_asia=MONO_FONT, size=base_size - 0.5,
                         color=CODE_RED)
        elif token.startswith("[") and "](" in token:
            label = token[1: token.index("]")]
            run = paragraph.add_run(label)
            set_run_font(run, name=base_font, east_asia=base_font, size=base_size, color=color)
        else:
            run = paragraph.add_run(token)
            set_run_font(run, name=base_font, east_asia=base_font, size=base_size, color=color)


# ---------------------------------------------------------------- 文档骨架


def setup_styles(document: Document) -> None:
    """正文与四级标题样式。"""
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.25
    pf.space_before = Pt(0)
    pf.space_after = Pt(2.5)

    specs = {
        "Heading 1": (18, NAVY, 18, 10),
        "Heading 2": (15, BLUE, 20, 8),
        "Heading 3": (12.5, NAVY, 12, 5),
        "Heading 4": (11, RGBColor(0x33, 0x33, 0x33), 10, 4),
    }
    for name, (size, color, before, after) in specs.items():
        style = document.styles[name]
        style.font.name = HEAD_FONT
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = color
        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        rfonts.set(qn("w:ascii"), HEAD_FONT)
        rfonts.set(qn("w:hAnsi"), HEAD_FONT)
        rfonts.set(qn("w:eastAsia"), HEAD_FONT)
        pf = style.paragraph_format
        pf.space_before = Pt(before)
        pf.space_after = Pt(after)
        pf.keep_with_next = True
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = 1.25
    # 章节不强制另起一页：本项目内容里表格/代码块较多，强制分页会让
    # 「只溢出一两行」的章节尾部留下一整页空白。改用加大标题前距 + 保持
    # 与后文同页来区分章节，既紧凑又不会出现空洞页。
    document.styles["Heading 2"].paragraph_format.page_break_before = False

    # 取消标题前的自动编号（Markdown 已带序号）
    for name in specs:
        ppr = document.styles[name].element.get_or_add_pPr()
        for old in ppr.findall(qn("w:numPr")):
            ppr.remove(old)


def setup_page(document: Document) -> None:
    """A4 + 页边距 + 页眉页脚（封面不显示）。"""
    section = document.sections[0]
    section.page_width = Cm(PAGE_W_CM)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(MARGIN_LR_CM)
    section.right_margin = Cm(MARGIN_LR_CM)
    section.header_distance = Cm(1.2)
    section.footer_distance = Cm(1.2)
    section.different_first_page_header_footer = True

    header = section.header
    hp = header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = hp.add_run("全能工具站 · 视频下载模块 PRD v1.0")
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=8.5, color=LIGHT_GREY)

    footer = section.footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r1 = fp.add_run("第 ")
    set_run_font(r1, name=HEAD_FONT, east_asia=HEAD_FONT, size=8.5, color=LIGHT_GREY)
    add_field(fp, "PAGE")
    r2 = fp.add_run(" 页 / 共 ")
    set_run_font(r2, name=HEAD_FONT, east_asia=HEAD_FONT, size=8.5, color=LIGHT_GREY)
    add_field(fp, "NUMPAGES")
    r3 = fp.add_run(" 页")
    set_run_font(r3, name=HEAD_FONT, east_asia=HEAD_FONT, size=8.5, color=LIGHT_GREY)


def add_rule(document: Document, *, color: str = "BFBFBF", space_before: int = 6,
             space_after: int = 10) -> None:
    """一条水平分隔线（用段落下边框实现）。"""
    p = document.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    para_border(p, side="bottom", color=color, size=6)


# ---------------------------------------------------------------- 封面 / 目录


def build_cover(document: Document, title: str, meta: list[tuple[str, str]]) -> None:
    """封面页。"""
    for _ in range(4):
        blank = document.add_paragraph()
        blank.paragraph_format.space_after = Pt(0)

    kicker = document.add_paragraph()
    kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = kicker.add_run("全 能 工 具 站   ·   T o o l K i t")
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=11, color=GREY)
    kicker.paragraph_format.space_after = Pt(26)

    tp = document.add_paragraph()
    tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = tp.add_run(title)
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=26, bold=True, color=NAVY)
    tp.paragraph_format.space_after = Pt(6)

    sub = document.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub.add_run("产 品 需 求 文 档 （ P R D ）")
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=13, color=BLUE)
    sub.paragraph_format.space_after = Pt(4)

    for _ in range(2):
        document.add_paragraph()

    add_rule(document, space_before=0, space_after=22)

    table = document.add_table(rows=0, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for label, value in meta:
        row = table.add_row()
        no_split(row)
        left, right = row.cells
        lp = left.paragraphs[0]
        lp.paragraph_format.space_after = Pt(0)
        add_inline(lp, label, base_size=10, base_font=HEAD_FONT, color=GREY)
        rp = right.paragraphs[0]
        rp.paragraph_format.space_after = Pt(0)
        add_inline(rp, value, base_size=10, base_font=HEAD_FONT, color=RGBColor(0x26, 0x26, 0x26))
    set_col_widths(table, [4.2, 9.8])
    table_borders(table, color="D9D9D9", size=4)
    table_cell_margins(table, top=70, bottom=70, left=110, right=110)

    for _ in range(3):
        document.add_paragraph()

    note = document.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = note.add_run("内部资料 · 评审稿 · 请勿外传")
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=9, color=LIGHT_GREY)

    document.add_page_break()


def build_toc(document: Document, headings: Iterable[tuple[int, str]]) -> None:
    """静态目录（章节大纲，不含页码）。"""
    h = document.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = h.add_run("目　　录")
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=18, bold=True, color=NAVY)
    h.paragraph_format.space_after = Pt(12)

    for level, text in headings:
        p = document.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Cm(0.0 if level == 1 else 0.8)
        # 用「固定行距」而不是倍率：中文字体自带较大的行高，倍率行距会把
        # 82 条目录顶到 3 页；固定行距能稳定压进 2 页且不裁字。
        p.paragraph_format.line_spacing = Pt(15 if level == 1 else 13)
        p.paragraph_format.keep_with_next = level == 1
        run = p.add_run(text)
        set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT,
                     size=10.5 if level == 1 else 9.0,
                     bold=(level == 1),
                     color=NAVY if level == 1 else RGBColor(0x40, 0x40, 0x40))
    document.add_page_break()


# ---------------------------------------------------------------- 内容块


def add_heading(document: Document, level: int, text: str):
    """2-4 级标题（1 级留给封面）。返回段落，便于就地覆盖样式行为。"""
    # Markdown 的 ## 对应章节，这里映射到 Heading 2 并保留文字自带序号
    p = document.add_paragraph(style=f"Heading {min(level, 4)}")
    run = p.add_run(clean(text))
    size = {2: 15, 3: 12.5, 4: 11}[min(level, 4)]
    color = {2: BLUE, 3: NAVY, 4: RGBColor(0x33, 0x33, 0x33)}[min(level, 4)]
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=size, bold=True, color=color)
    return p


def add_paragraph(document: Document, text: str) -> None:
    """正文段落（首行缩进两字）。"""
    p = document.add_paragraph()
    p.paragraph_format.first_line_indent = Pt(21)
    add_inline(p, text, base_size=10.5)


def add_bullet(document: Document, text: str, *, level: int = 1) -> None:
    """无序列表项。"""
    p = document.add_paragraph()
    indent = 0.6 + 0.55 * (level - 1)
    p.paragraph_format.left_indent = Cm(indent)
    p.paragraph_format.first_line_indent = Cm(-0.4)
    mark = "▪ " if level == 1 else "– "
    run = p.add_run(mark)
    set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=10.5, color=BLUE)
    add_inline(p, text, base_size=10.5)


def add_checkbox(document: Document, text: str) -> None:
    """任务清单项。"""
    p = document.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.9)
    p.paragraph_format.first_line_indent = Cm(-0.5)
    run = p.add_run("☐  ")
    set_run_font(run, name="Segoe UI Symbol", east_asia=HEAD_FONT, size=11, color=BLUE)
    add_inline(p, text, base_size=10.5)


def add_numbered(document: Document, text: str) -> None:
    """有序列表项：保留原文序号，做成悬挂缩进。"""
    p = document.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.95)
    p.paragraph_format.first_line_indent = Cm(-0.65)
    add_inline(p, text, base_size=10.5)


def add_quote(document: Document, text: str) -> None:
    """引用块。"""
    p = document.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.8)
    p.paragraph_format.right_indent = Cm(0.4)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    para_border(p, side="left", color="2E5496", size=18)
    add_inline(p, text, base_size=9.5, color=GREY)


def add_code_block(document: Document, lines: list[str]) -> None:
    """代码/图示块：单格表格 + 底纹 + 等宽字体，保证不被拆页且不裁切。"""
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    cell = table.rows[0].cells[0]
    cell.width = Cm(CONTENT_W_CM)
    set_col_widths(table, [CONTENT_W_CM])
    table_borders(table, color="D9D9D9", size=4)
    table_cell_margins(table, top=110, bottom=110, left=170, right=120)
    cell_shade(cell, "F7F7F7")
    cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP

    first = cell.paragraphs[0]
    first.paragraph_format.space_before = Pt(0)
    first.paragraph_format.space_after = Pt(0)
    first.paragraph_format.line_spacing = 1.0
    for index, line in enumerate(lines):
        paragraph = first if index == 0 else cell.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1.0
        paragraph.paragraph_format.keep_with_next = index < len(lines) - 1
        run = paragraph.add_run(clean(line).replace("\t", "    ") or " ")
        set_run_font(run, name=MONO_FONT, east_asia=BODY_FONT, size=8.5,
                     color=RGBColor(0x26, 0x26, 0x26))
    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)
    spacer.paragraph_format.line_spacing = 1.0
    spacer_run = spacer.add_run("")
    set_run_font(spacer_run, name=BODY_FONT, east_asia=BODY_FONT, size=4)


def visible_len(text: str) -> int:
    """粗略可见宽度：CJK 记 2，ASCII 记 1。"""
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0x2E80 else 1
    return total


def add_table(document: Document, rows: list[list[str]]) -> None:
    """管道表格：首行表头，自动列宽，表头跨页重复，隔行底纹。"""
    cols = max(len(r) for r in rows)
    normalized = [r + [""] * (cols - len(r)) for r in rows]

    # 列宽按内容宽度加权
    weights = []
    for index in range(cols):
        longest = max(visible_len(normalized[r][index]) for r in range(len(normalized)))
        weights.append(min(max(longest, 6), 34))
    total_weight = sum(weights)
    widths = [CONTENT_W_CM * w / total_weight for w in weights]
    widths = [max(1.7, w) for w in widths]
    scale = CONTENT_W_CM / sum(widths)
    widths = [w * scale for w in widths]

    table = document.add_table(rows=0, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table_borders(table, color="BFBFBF", size=4)
    table_cell_margins(table, top=45, bottom=45, left=90, right=90)

    for r_index, row_data in enumerate(normalized):
        row = table.add_row()
        no_split(row)
        if r_index == 0:
            repeat_header(row)
        for c_index, text in enumerate(row_data):
            cell = row.cells[c_index]
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.15
            if r_index == 0:
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                cell_shade(cell, "1F3864")
                run = paragraph.add_run(clean(text))
                set_run_font(run, name=HEAD_FONT, east_asia=HEAD_FONT, size=9, bold=True,
                             color=RGBColor(0xFF, 0xFF, 0xFF))
            else:
                cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
                if r_index % 2 == 0:
                    cell_shade(cell, "F2F5FA")
                add_inline(paragraph, text, base_size=9, color=RGBColor(0x26, 0x26, 0x26))

    set_col_widths(table, widths)

    # 小表整体保持在同一页：避免「表头 + 1 行」被孤零零留在页脚。
    # 大表（> 9 行）允许跨页，并靠表头重复保证可读性。
    if len(normalized) <= 9:
        for row in table.rows[:-1]:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_with_next = True

    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(4)
    spacer.paragraph_format.line_spacing = 1.0


# ---------------------------------------------------------------- 解析主循环


def is_separator(line: str) -> bool:
    """判断表格分隔行 |---|---|。"""
    stripped = line.strip()
    if not stripped.startswith("|") or "-" not in stripped:
        return False
    return bool(re.fullmatch(r"\|[\s:\-|]+\|?", stripped))


def split_row(line: str) -> list[str]:
    """拆分管道表格行。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def collect_headings(lines: list[str]) -> list[tuple[int, str]]:
    """预扫标题，供目录使用（## 为一级，### 为二级）。"""
    result: list[tuple[int, str]] = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = re.match(r"^(#{2,3})\s+(.*)$", line)
        if match:
            level = len(match.group(1)) - 1
            result.append((level, match.group(2).strip()))
    return result


def build(md_path: Path, docx_path: Path) -> None:
    """主流程。"""
    lines = md_path.read_text(encoding="utf-8").splitlines()
    document = Document()
    setup_page(document)
    setup_styles(document)

    title = "短视频作品链接自动下载"
    body_start = 0
    for index, line in enumerate(lines):
        match = re.match(r"^#\s+(.*)$", line)
        if match:
            title = match.group(1).strip()
            body_start = index + 1
            break

    meta = [
        ("文档名称", "短视频作品链接自动下载 产品需求文档"),
        ("所属产品", "全能工具站（ToolKit）v1.0"),
        ("模块标识", "video"),
        ("文档版本", "v1.0（评审稿）"),
        ("文档状态", "待评审"),
        ("目标版本", "ToolKit v1.1.0"),
        ("适用端", "Web（桌面为主，移动端自适应）"),
        ("编写日期", "2026-10-20"),
        ("需求来源", "用户反馈 + 现有模块能力补齐"),
    ]
    build_cover(document, title, meta)
    build_toc(document, collect_headings(lines))

    index = body_start
    total = len(lines)
    skipped_meta = False
    while index < total:
        raw = lines[index]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            index += 1
            block: list[str] = []
            while index < total and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            add_code_block(document, block)
            continue

        if stripped.startswith("|") and index + 1 < total and is_separator(lines[index + 1]):
            rows: list[list[str]] = [split_row(stripped)]
            index += 2
            while index < total and lines[index].strip().startswith("|"):
                rows.append(split_row(lines[index]))
                index += 1
            # 正文开头的「文档信息表」与封面信息重复；跳过它，避免扉页把
            # 修订记录与阅读指引挤到下一页、留下一张几乎空白的页。
            if not skipped_meta and [c.strip() for c in rows[0]] == ["项目", "内容"]:
                skipped_meta = True
                continue
            add_table(document, rows)
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            # 分隔线交给标题间距与表格留白，不额外画线：
            # 空段落上的 pBdr 在 LibreOffice 里只会渲染出一小截横线。
            index += 1
            continue

        match = re.match(r"^(#{2,4})\s+(.*)$", stripped)
        if match:
            heading_text = match.group(2).strip()
            heading = add_heading(document, len(match.group(1)), heading_text)
            # 扉页信息与阅读指引紧跟文档信息表，单独占页会留下大片空白
            if heading_text.startswith(("修订记录", "0. 阅读指引")):
                heading.paragraph_format.page_break_before = False
            index += 1
            continue

        if re.match(r"^-\s+\[[ xX]\]\s+", stripped):
            add_checkbox(document, re.sub(r"^-\s+\[[ xX]\]\s+", "", stripped))
            index += 1
            continue

        nested = re.match(r"^(\s{2,})-\s+(.*)$", line)
        if nested:
            add_bullet(document, nested.group(2), level=2)
            index += 1
            continue

        if re.match(r"^-\s+", stripped):
            add_bullet(document, re.sub(r"^-\s+", "", stripped))
            index += 1
            continue

        if re.match(r"^\d+\.\s+", stripped):
            add_numbered(document, stripped)
            index += 1
            continue

        if stripped.startswith(">"):
            add_quote(document, re.sub(r"^>\s?", "", stripped))
            index += 1
            continue

        add_paragraph(document, stripped)
        index += 1

    properties = document.core_properties
    properties.title = title
    properties.subject = "产品需求文档（PRD）"
    properties.author = "ToolKit 产品组"
    properties.category = "需求文档"

    docx_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(docx_path))


if __name__ == "__main__":
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    build(source, target)
    print(f"OK -> {target} ({target.stat().st_size} bytes)")
