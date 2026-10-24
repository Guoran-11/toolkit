"""Markdown 模块路由：``/api/md/export``。

契约来源：``app/static/js/api.js`` 的 ``mdApi.export`` 与 ``views/markdown.js``。

请求体（JSON）：``{"html": "<完整 HTML 文档>", "filename": "xxx.md", "format": "pdf" | "html"}``

返回形态刻意分为两种，与前端 mock.mdExport 的实现保持一致
（前端逻辑：``if (task.status === 'success' && task.output)`` 直接下载，否则轮询）：
    · ``format=html`` —— **同步完成**，返回 ``status='success'`` 的终态任务对象。
      纯文本落盘，没有任何引擎依赖，让它排队反而增加无谓延迟。
    · ``format=pdf``  —— **异步任务**，返回 ``status='pending'`` 的任务对象，
      由前端轮询。因为 PDF 需要拉起 Chromium，属于重任务，必须排队。

前端 Markdown 渲染本身走浏览器本地（FR-MD-10），本模块只负责导出。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ClientIpDep, ConfigDep, GuardDep
from app.core.config import TEMP_DIR
from app.core.errors import ValidationError
from app.core.security import write_audit_log
from app.core.storage import registry, safe_filename, stage_output
from app.core.tasks import (
    JobContext,
    JobFunc,
    JobResult,
    TaskInput,
    get_task_manager,
)
from app.modules.markdown.renderer import ensure_safe_document, extract_title

logger = logging.getLogger(__name__)

router = APIRouter(tags=["markdown"])

# 单次导出的 HTML 体积上限（字符数）。
# 前端组装的 HTML 已内嵌 Base64 图片与渲染结果，10 MB 足以覆盖超长文档，
# 同时避免有人用超大 payload 打爆内存。
MAX_HTML_CHARS: int = 10 * 1024 * 1024


class MarkdownExportRequest(BaseModel):
    """Markdown 导出请求。

    Attributes:
        html: 前端 ``buildExportHtml()`` 生成的完整 HTML。
        filename: 源 Markdown 文件名（用于推导产物名）。
        format: 导出格式（``pdf`` 或 ``html``）。
    """

    model_config = {"extra": "allow"}

    html: str = Field(default="", description="待导出的完整 HTML")
    filename: str = Field(default="document.md", max_length=260)
    format: str = Field(default="pdf")

    @property
    def normalized_format(self) -> str:
        """规范化格式标识。

        Returns:
            ``pdf`` 或 ``html``。

        Raises:
            ValidationError: 格式不受支持。
        """
        fmt = (self.format or "pdf").strip().lower()
        if fmt not in {"pdf", "html"}:
            raise ValidationError(f"不支持的导出格式：{self.format}。当前支持 pdf 与 html。")
        return fmt

    @property
    def stem(self) -> str:
        """产物名主干。

        Returns:
            去除扩展名并清洗后的文件名主干。
        """
        cleaned = safe_filename(self.filename or "document.md", fallback="document")
        stem, dot, _ = cleaned.rpartition(".")
        return stem if dot and stem else cleaned


def make_export_job(*, html: str, stem: str) -> JobFunc:
    """构造 PDF 导出任务函数。

    Args:
        html: 已净化的完整 HTML。
        stem: 产物名主干。

    Returns:
        符合 ``JobFunc`` 签名的闭包。
    """

    def job(ctx: JobContext) -> JobResult:
        """执行 PDF 导出。

        Args:
            ctx: 任务上下文。

        Returns:
            任务产出描述。
        """
        workdir = Path(tempfile.mkdtemp(prefix="md-export-", dir=str(TEMP_DIR)))
        try:
            ctx.progress(15)
            html_path = workdir / "source.html"
            html_path.write_text(html, encoding="utf-8")

            from app.modules.converter.base import ConversionParams
            from app.modules.converter.html2pdf import render_html_to_pdf

            ctx.progress(35)
            output = render_html_to_pdf(
                html_path,
                workdir,
                params=ConversionParams(paper_size="A4", margin_mm=16.0, print_background=True),
                ctx=ctx,
            )

            # 产物改名，让下载文件名对用户有意义
            named = workdir / f"{stem}.pdf"
            shutil.move(str(output), str(named))
            ctx.progress(95)
            # 必须先把产物移出 workdir：任务函数返回后任务管理器才登记产物，
            # 而下面的 finally 会立刻删除整个 workdir（否则登记必然 FileNotFoundError）。
            staged = stage_output(named)
            return JobResult(path=staged, name=named.name, mime="application/pdf")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return job


@router.post("/api/md/export")
def markdown_export(
    payload: MarkdownExportRequest,
    settings: ConfigDep,
    _user: GuardDep,
    ip: ClientIpDep,
) -> dict[str, Any]:
    """导出 Markdown 渲染结果为 PDF 或 HTML。

    Args:
        payload: 导出请求。
        settings: 全局配置。
        _user: 鉴权主体。
        ip: 客户端 IP（审计日志）。

    Returns:
        任务对象；``format=html`` 时为终态（success），``format=pdf`` 时为 pending。

    Raises:
        ValidationError: 内容为空或超出体积上限。
    """
    if not (payload.html or "").strip():
        raise ValidationError("导出内容为空，请先加载 Markdown 文档。")

    html_chars = len(payload.html)
    if html_chars > MAX_HTML_CHARS:
        raise ValidationError(
            f"导出内容过大（{html_chars // 1024 // 1024} MB），"
            f"上限为 {MAX_HTML_CHARS // 1024 // 1024} MB。请减少内嵌图片或拆分文档。"
        )

    fmt = payload.normalized_format
    title = extract_title(payload.html, default=payload.stem)

    # 服务端独立净化（防御纵深，见 renderer 模块 docstring）
    try:
        safe_html, report = ensure_safe_document(payload.html, title=title)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    if report.get("removed_total"):
        logger.info("Markdown 导出净化：移除 %s 项危险构造", report["removed_total"])

    ttl_hours = settings.limits.output_ttl_hours

    # ---------------- HTML：同步完成 ----------------
    if fmt == "html":
        workdir = Path(tempfile.mkdtemp(prefix="md-html-", dir=str(TEMP_DIR)))
        try:
            output = workdir / f"{payload.stem}.html"
            output.write_text(safe_html, encoding="utf-8")
            registered = registry.register(
                output,
                name=f"{payload.stem}.html",
                mime="text/html;charset=utf-8",
                ttl_hours=ttl_hours,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        # 同步完成 ≠ 不记录。必须登记进任务表，否则会有两个用户可见的缺陷：
        #   1. 刷新页面后任务从「任务中心」消失（前端从 /api/tasks 重新拉取）；
        #   2. 该 task_id 无法被 /api/convert/batch-zip 解析，选中后打包必然失败。
        record = get_task_manager().register_completed(
            module="markdown",
            operation="md2html",
            inputs=[
                TaskInput(
                    name=safe_filename(payload.filename or "document.md", fallback="document.md"),
                    size=html_chars,
                    sha256="",
                )
            ],
            output=registered,
        )

        write_audit_log({"action": "md.export", "ip": ip, "format": "html", "name": payload.stem})
        return record.to_api()

    # ---------------- PDF：进入任务队列 ----------------
    manager = get_task_manager()
    record = manager.submit(
        module="markdown",
        operation="md2pdf",
        inputs=[
            TaskInput(
                name=safe_filename(payload.filename or "document.md", fallback="document.md"),
                size=html_chars,
                sha256="",
            )
        ],
        job=make_export_job(html=safe_html, stem=payload.stem),
    )

    write_audit_log({"action": "md.export", "ip": ip, "format": "pdf", "name": payload.stem})
    return record.to_api()


__all__ = ["router"]
