"""转换任务的公共上下文与参数模型。

所有具体转换实现（office / pdf / html2pdf）都接收 ``ConversionRequest``，
从而与 FastAPI 层和任务层解耦：单元测试可以直接构造该对象来验证转换逻辑。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.tasks import JobContext


@dataclass(slots=True)
class ConversionParams:
    """转换参数（来自前端 ``params`` 字段，全部为可选）。

    字段名与前端 ``matrix.js`` 的 ``TARGET_PARAMS`` 一一对应，
    因此这里刻意保留 snake_case，不做驼峰转换。

    Attributes:
        paper_size: 纸张大小（A4 / Letter / A3 / A5）。
        orientation: 方向（portrait / landscape）。
        margin_mm: 页边距（毫米）。
        print_background: 是否打印背景色与图片。
        scale: 页面缩放百分比。
        dpi: 图片输出分辨率（96 / 150 / 300）。
        image_format: 图片格式（png / jpg）。
        quality: JPG 质量（40–100）。
        page_range: 页面范围（all 或 1-3,5,8）。
        keep_layout: PDF→DOCX 时是否尽量保留版面。
        table_mode: 表格识别模式（auto / lattice / stream）。
        one_sheet_per_table: 每个表格一个工作表。
        slide_size: 幻灯片尺寸（16:9 / 4:3）。
        note_mode: 图片模式时是否把原文放入备注。
        inline_css: HTML 输出是否内联样式。
        embed_images: HTML 输出是否内嵌图片（Base64）。
    """

    paper_size: str = "A4"
    orientation: str = "portrait"
    margin_mm: float = 12.0
    print_background: bool = True
    scale: float = 100.0
    dpi: int = 150
    image_format: str = "png"
    quality: int = 90
    page_range: str = "all"
    keep_layout: bool = True
    table_mode: str = "auto"
    one_sheet_per_table: bool = True
    slide_size: str = "16:9"
    note_mode: bool = True
    inline_css: bool = True
    embed_images: bool = False

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> ConversionParams:
        """从任意前端字典构造参数对象。

        未知键被忽略，类型不符时回退到默认值——参数错误绝不应该让任务失败，
        因为前端表单在版本升级后可能多传字段。

        Args:
            raw: 前端提交的参数字典。

        Returns:
            ConversionParams 实例。
        """
        raw = raw or {}
        defaults = cls()
        values: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            if name not in raw:
                continue
            current = getattr(defaults, name)
            candidate = raw[name]
            try:
                if isinstance(current, bool):
                    values[name] = (
                        candidate
                        if isinstance(candidate, bool)
                        else str(candidate).lower() in {"true", "1", "yes", "on"}
                    )
                elif isinstance(current, int):
                    values[name] = int(float(candidate))
                elif isinstance(current, float):
                    values[name] = float(candidate)
                else:
                    values[name] = str(candidate)
            except (TypeError, ValueError):
                continue
        return cls(**values)

    def effective_dpi(self) -> int:
        """归一化 DPI 到受支持的档位。

        Returns:
            96 / 150 / 300 之一。
        """
        allowed = (96, 150, 300)
        try:
            value = int(self.dpi)
        except (TypeError, ValueError):
            return 150
        return min(allowed, key=lambda item: abs(item - value))

    def parsed_page_range(self, total_pages: int) -> list[int]:
        """解析页面范围表达式为 0 基索引列表。

        支持 ``all`` / 空 / ``1-3,5,8`` 形式。越界索引会被裁掉而不是报错——
        用户写 ``1-100`` 而文档只有 3 页时，合理行为是输出全部 3 页。

        Args:
            total_pages: 文档总页数。

        Returns:
            0 基页索引列表（升序去重）。
        """
        text = (self.page_range or "all").strip().lower()
        if not text or text == "all":
            return list(range(total_pages))

        pages: set[int] = set()
        for chunk in text.replace("，", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "-" in chunk:
                start_text, _, end_text = chunk.partition("-")
                try:
                    start = int(start_text.strip() or 1)
                    end = int(end_text.strip() or total_pages)
                except ValueError:
                    continue
                for page in range(min(start, end), max(start, end) + 1):
                    if 1 <= page <= total_pages:
                        pages.add(page - 1)
            else:
                try:
                    page = int(chunk)
                except ValueError:
                    continue
                if 1 <= page <= total_pages:
                    pages.add(page - 1)

        return sorted(pages) if pages else list(range(total_pages))


@dataclass(slots=True)
class ConversionRequest:
    """一次转换请求的完整上下文。

    Attributes:
        source_path: 已落盘的源文件（UUID 命名）。
        source_format: 源格式 id。
        target_format: 目标格式 id。
        params: 转换参数。
        display_name: 用户可见的原始文件名（用于生成产物名）。
        workdir: 本次任务独占的临时目录（任务结束由调用方清理）。
        ctx: 任务上下文（进度与取消）。
    """

    source_path: Path
    source_format: str
    target_format: str
    params: ConversionParams
    display_name: str
    workdir: Path
    ctx: JobContext

    def output_path(self, suffix: str | None = None) -> Path:
        """在任务临时目录中分配一个产出路径。

        Args:
            suffix: 扩展名（不含点）；None 表示使用目标格式的默认扩展名。

        Returns:
            绝对路径。
        """
        from app.modules.converter.matrix import TARGET_SUFFIX

        ext = suffix or TARGET_SUFFIX.get(self.target_format, "bin")
        return self.workdir / f"output.{ext}"

    def stem(self) -> str:
        """用户可见的文件名主干（不含扩展名）。

        Returns:
            形如 ``报告`` 的主干名。
        """
        name = self.display_name or "output"
        stem, dot, _ = name.rpartition(".")
        return stem if dot and stem else name

    def output_name(self, suffix: str | None = None) -> str:
        """生成对用户可见的产物文件名。

        Args:
            suffix: 扩展名（不含点）；None 表示使用目标格式默认扩展名。

        Returns:
            形如 ``报告.pdf`` 的文件名。
        """
        from app.modules.converter.matrix import TARGET_SUFFIX

        ext = suffix or TARGET_SUFFIX.get(self.target_format, "bin")
        return f"{self.stem()}.{ext}"

    @classmethod
    def create_workdir(cls) -> Path:
        """创建一个任务级临时目录。

        Returns:
            新建目录路径（位于 storage/temp 下，便于统一清理）。
        """
        from app.core.config import TEMP_DIR

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="job-", dir=str(TEMP_DIR)))


@dataclass(slots=True)
class ConversionOutcome:
    """转换产出。

    Attributes:
        path: 产出文件路径。
        name: 对用户可见的文件名。
        mime: MIME 类型。
        extra_notes: 需要提示给用户的附加说明（如"已降级为图片模式"）。
    """

    path: Path
    name: str
    mime: str
    extra_notes: list[str] = field(default_factory=list)


__all__ = ["ConversionOutcome", "ConversionParams", "ConversionRequest"]
