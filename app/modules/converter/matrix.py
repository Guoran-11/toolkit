"""文档转换支持矩阵与路径分派。

矩阵内容与前端 ``app/static/js/matrix.js`` 的 ``MATRIX`` 常量**逐条对齐**：
前端在离线态用它渲染界面，后端在线态返回同一份数据供前端校准。
两边必须保持同源，否则会出现「界面说支持、提交后报不支持」的割裂。

质量等级语义（务必如实呈现，风险 R5 明确要求前置提示）：
    A —— 结构级还原，输出可编辑且版式准确
    B —— 尽力还原，可能存在版式偏差
    C —— 视觉保真，输出为图片/整页对象，不可逐字编辑
    None —— 不支持（界面显示 ×）；源目标相同显示 —
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Grade = Literal["A", "B", "C"]

# 前端 SOURCE_FORMATS / TARGET_FORMATS 的 id 序列
SOURCE_FORMATS: Final[tuple[str, ...]] = ("html", "docx", "xlsx", "pptx", "pdf")
TARGET_FORMATS: Final[tuple[str, ...]] = ("docx", "xlsx", "pptx", "pdf", "html", "png")


@dataclass(frozen=True, slots=True)
class MatrixCell:
    """矩阵中的一个格子。

    Attributes:
        grade: 质量等级；None 表示不支持。
        engine: 展示给用户的引擎名（中文可读）。
        note: 预期质量说明（界面直接展示）。
    """

    grade: Grade | None
    engine: str | None
    note: str

    def to_api(self) -> dict[str, object]:
        """转换为前端 lookup() 可消费的结构。

        Returns:
            含 ``grade`` / ``engine`` / ``note`` 的字典。
        """
        return {"grade": self.grade, "engine": self.engine, "note": self.note}


def _cell(grade: Grade | None, engine: str | None, note: str = "") -> MatrixCell:
    """构造矩阵格子的简写。

    Args:
        grade: 质量等级。
        engine: 引擎展示名。
        note: 说明文案。

    Returns:
        MatrixCell 实例。
    """
    return MatrixCell(grade=grade, engine=engine, note=note)


# 与前端 matrix.js 的 MATRIX 完全一致
MATRIX: Final[dict[str, dict[str, MatrixCell]]] = {
    "html": {
        "docx": _cell("B", "mammoth", "提取正文与基础样式，复杂 CSS 布局会简化"),
        "xlsx": _cell("B", "表格区域识别", "识别页面中的表格并转为工作表，非表格内容将丢弃"),
        "pptx": _cell(None, None, "HTML 的流式布局无法映射为幻灯片版面，不支持"),
        "pdf": _cell("A", "Playwright + Chromium", "与浏览器打印预览高度一致，保留打印 CSS、图片与字体"),
        "html": _cell(None, None, ""),
        "png": _cell("A", "Chromium 截图", "整页光栅化，长页面会按视口分段截图"),
    },
    "docx": {
        "docx": _cell(None, None, ""),
        "xlsx": _cell("B", "表格提取", "表格转为工作表，正文段落另存一表；公式与图表不保留"),
        "pptx": _cell("C", "LibreOffice → 逐页导出", "每页一张图片，文字不可编辑"),
        "pdf": _cell("A", "LibreOffice headless", "版面还原度高，页眉页脚与分页保留"),
        "html": _cell("A", "mammoth", "结构清晰、语义化 HTML，样式经过简化"),
        "png": _cell("C", "LibreOffice → PDF → 光栅化", "经 PDF 中转，输出为图片，不可编辑"),
    },
    "xlsx": {
        "docx": _cell("B", "经 HTML 中转", "工作表转为表格，公式与图表不保留"),
        "xlsx": _cell(None, None, ""),
        "pptx": _cell(None, None, "表格数据无法映射为幻灯片版面，不支持"),
        "pdf": _cell("A", "LibreOffice headless", "保留列宽、分页与打印区域设置"),
        "html": _cell("A", "openpyxl", "每个工作表输出为一个 HTML 表格"),
        "png": _cell("C", "LibreOffice → PDF → 光栅化", "经 PDF 中转，长表格会分页输出为多张图片"),
    },
    "pptx": {
        "docx": _cell("B", "备注 + 图片", "仅导出备注文字与每页缩略图，版式不保留"),
        "xlsx": _cell("B", "表格提取", "幻灯片表格与文本分表导出，版式不保留"),
        "pptx": _cell(None, None, ""),
        "pdf": _cell("A", "LibreOffice headless", "与放映效果基本一致，动画与切换效果不保留"),
        "html": _cell("C", "逐页图片", "每页一张图片，文字不可选中"),
        "png": _cell("C", "LibreOffice → PDF → 光栅化", "每页输出一张图片"),
    },
    "pdf": {
        "docx": _cell("B", "pdf2docx", "重建段落与表格结构，可在 Word 中编辑；扫描件需先 OCR"),
        "xlsx": _cell("B", "pdfplumber 表格识别", "自动识别表格区域，非表格内容会丢失；扫描件需先 OCR"),
        "pptx": _cell("C", "PyMuPDF 逐页光栅化", "⚠ 输出为每页一张图片，文字不可编辑"),
        "pdf": _cell(None, None, ""),
        "html": _cell("B", "pdfminer.six", "保留可选中文本与基本布局，复杂版面会重排"),
        "png": _cell("A", "PyMuPDF", "按页光栅化，DPI 可选，输出可打包 ZIP"),
    },
}

# 扩展名映射（用于推断源格式与产物扩展名）
SUFFIX_TO_FORMAT: Final[dict[str, str]] = {
    "html": "html",
    "htm": "html",
    "mhtml": "html",
    "mht": "html",
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "pdf": "pdf",
}

# 目标格式 → 产物扩展名
TARGET_SUFFIX: Final[dict[str, str]] = {
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "pdf": "pdf",
    "html": "html",
    "png": "png",
}

# 目标格式 → MIME
TARGET_MIME: Final[dict[str, str]] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "html": "text/html;charset=utf-8",
    "png": "image/png",
}

# 各源格式允许的扩展名（上传白名单，配合 magic bytes 二次校验）
SOURCE_SUFFIXES: Final[dict[str, tuple[str, ...]]] = {
    "html": ("html", "htm", "mhtml", "mht"),
    "docx": ("docx",),
    "xlsx": ("xlsx",),
    "pptx": ("pptx",),
    "pdf": ("pdf",),
}

# 各源格式的 magic 类型族白名单
SOURCE_MAGIC_KINDS: Final[dict[str, tuple[str, ...]]] = {
    "html": ("text",),  # HTML 无固定 magic，走文本可解码性检查
    "docx": ("zip",),  # OOXML 均为 zip 容器
    "xlsx": ("zip",),
    "pptx": ("zip",),
    "pdf": ("pdf",),
}


def lookup(source: str | None, target: str | None) -> MatrixCell | None:
    """查询某个转换组合的规格。

    Args:
        source: 源格式 id。
        target: 目标格式 id。

    Returns:
        MatrixCell；组合不存在返回 None。
    """
    if not source or not target:
        return None
    row = MATRIX.get(source)
    if not row:
        return None
    return row.get(target)


def is_supported(source: str, target: str) -> bool:
    """判断转换方向是否受支持。

    Args:
        source: 源格式 id。
        target: 目标格式 id。

    Returns:
        受支持返回 True。
    """
    cell = lookup(source, target)
    return bool(cell and cell.grade)


def supported_targets(source: str) -> list[str]:
    """某源格式支持的全部目标格式。

    Args:
        source: 源格式 id。

    Returns:
        目标格式 id 列表。
    """
    row = MATRIX.get(source) or {}
    return [key for key, cell in row.items() if cell.grade]


def format_from_suffix(filename: str) -> str | None:
    """按扩展名推断源格式。

    Args:
        filename: 文件名。

    Returns:
        源格式 id；无法识别返回 None。
    """
    _, _, ext = (filename or "").rpartition(".")
    return SUFFIX_TO_FORMAT.get(ext.lower()) if ext else None


def matrix_api_payload() -> dict[str, object]:
    """构造 ``GET /api/convert/matrix`` 的响应体。

    结构与 mock.matrix() 保持一致，前端可直接消费：
    ``{sources, targets, matrix, engines}``。

    Returns:
        响应字典；``engines`` 由调用方注入。
    """
    return {
        "sources": list(SOURCE_FORMATS),
        "targets": list(TARGET_FORMATS),
        "matrix": {
            source: {target: cell.to_api() for target, cell in row.items()} for source, row in MATRIX.items()
        },
    }


__all__ = [
    "MATRIX",
    "SOURCE_FORMATS",
    "SOURCE_MAGIC_KINDS",
    "SOURCE_SUFFIXES",
    "TARGET_FORMATS",
    "TARGET_MIME",
    "TARGET_SUFFIX",
    "MatrixCell",
    "format_from_suffix",
    "is_supported",
    "lookup",
    "matrix_api_payload",
    "supported_targets",
]
