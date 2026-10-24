"""HTML → PDF / 图片 渲染通道（Playwright + Chromium）。

为什么选 Playwright 而不是 WeasyPrint：
    - WeasyPrint 不支持完整 CSS（Flex/Grid 支持残缺），且中文字体需额外配置；
    - Playwright 直接复用 Chromium 的排版引擎，与「浏览器打印预览」天然一致，
      这正好是 AC-01 的验收口径（像素级基本一致）。

Playwright 的同步 API 在**已有事件循环的线程里不能调用**。
FastAPI 的 async 路由跑在事件循环线程上，而本模块由任务线程池中的 worker 线程调用，
那里没有运行中的事件循环，因此使用 sync_api 是安全且更简单的选择。
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Final

from app.core.engines import engine_probe
from app.core.errors import ConversionFailedError, EngineUnavailableError
from app.core.tasks import JobContext

logger = logging.getLogger(__name__)

# CSS 毫米 → 英寸（Playwright 的 margin 单位为英寸）
_MM_PER_INCH: Final[float] = 25.4

# 纸张尺寸表（毫米），与前端 TARGET_PARAMS 的选项一致
PAPER_SIZES: Final[dict[str, tuple[float, float]]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A5": (148.0, 210.0),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
}

# 打印时统一注入的基础样式：
# -webkit-print-color-adjust 保证背景色在 PDF 中保留（AC-01 明确要求不丢背景/图片）
_PRINT_RESET_CSS: Final[str] = """
@media print {
  *, *::before, *::after {
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }
  html, body { margin: 0 !important; padding: 0 !important; }
  img, svg, table, pre, blockquote { break-inside: avoid; page-break-inside: avoid; }
  h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
}
"""


def _require_chromium() -> None:
    """确认 Chromium 可用。

    Raises:
        EngineUnavailableError: Playwright 或 Chromium 缺失，附安装指引。
    """
    status = engine_probe.status_of("chromium")
    if status.state == "fail":
        raise EngineUnavailableError(
            "HTML 渲染引擎（Chromium）不可用。" + (status.hint or "请执行 playwright install chromium。")
        )


def _paper_dimensions(paper_size: str, orientation: str) -> dict[str, str] | None:
    """把纸张与方向换算为 Playwright 的 format 或宽高。

    Args:
        paper_size: 纸张名（A4 / Letter / ...）。
        orientation: portrait 或 landscape。

    Returns:
        ``page.pdf()`` 可直接展开的关键字参数字典。
    """
    size = PAPER_SIZES.get(paper_size) or PAPER_SIZES["A4"]
    width_mm, height_mm = size
    if orientation == "landscape":
        width_mm, height_mm = height_mm, width_mm
    return {"width": f"{width_mm}mm", "height": f"{height_mm}mm"}


def _build_pdf_options(params: Any, *, print_background: bool | None = None) -> dict[str, Any]:
    """构造 ``page.pdf()`` 的参数。

    Args:
        params: ConversionParams 实例。
        print_background: 覆盖「打印背景」设置。

    Returns:
        Playwright 关键字参数字典。
    """
    margin_mm = max(0.0, float(getattr(params, "margin_mm", 12.0)))
    margin = f"{margin_mm}mm"
    scale = float(getattr(params, "scale", 100.0)) / 100.0
    # Chromium 的 scale 取值范围是 0.1–2，超出会被静默忽略
    scale = min(2.0, max(0.1, scale))

    options: dict[str, Any] = {
        "print_background": getattr(params, "print_background", True)
        if print_background is None
        else print_background,
        "margin": {"top": margin, "right": margin, "bottom": margin, "left": margin},
        "scale": scale,
        "prefer_css_page_size": False,
    }
    options.update(
        _paper_dimensions(getattr(params, "paper_size", "A4"), getattr(params, "orientation", "portrait"))
    )
    return options


def render_html_to_pdf(
    html_path: Path,
    workdir: Path,
    *,
    params: Any,
    base_url: str | None = None,
    timeout_ms: int = 60_000,
    ctx: JobContext | None = None,
) -> Path:
    """把本地 HTML 文件渲染为 PDF。

    Args:
        html_path: HTML 文件路径。
        workdir: 输出目录。
        params: 转换参数（纸张、方向、页边距、缩放、打印背景）。
        base_url: 资源基准 URL；传 ``file://`` 目录可让相对路径图片正常加载。
        timeout_ms: 渲染超时（毫秒）。
        ctx: 任务上下文。

    Returns:
        PDF 文件路径。

    Raises:
        EngineUnavailableError: Chromium 不可用。
        ConversionFailedError: 渲染失败（含页面脚本死循环等）。
    """
    _require_chromium()
    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / "output.pdf"

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    options = _build_pdf_options(params)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",  # 容器内 /dev/shm 过小会导致崩溃
                    "--font-render-hinting=none",  # 关闭字体 hinting，避免中文笔画粗细跳变
                ]
            )
            try:
                page = browser.new_page()
                # 注入打印样式：保证背景色输出与分页控制
                if ctx:
                    ctx.progress(30)

                page.goto(
                    base_url if base_url else html_path.resolve().as_uri(),
                    wait_until="load",
                    timeout=timeout_ms,
                )
                # 等网络空闲，给图表库（Mermaid / KaTeX）留出渲染时间
                # 有持续请求（如动画）时 networkidle 永远达不到，不算失败
                with contextlib.suppress(PlaywrightTimeoutError):
                    page.wait_for_load_state("networkidle", timeout=10_000)

                page.add_style_tag(content=_PRINT_RESET_CSS)
                page.emulate_media(media="print")

                if ctx:
                    ctx.progress(60)

                page.pdf(path=str(output), **options)
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise ConversionFailedError(
            "渲染超时。页面可能包含无法加载的外部资源或死循环脚本，"
            "请确认 HTML 中的图片/字体均为本地相对路径。"
        ) from exc
    except PlaywrightError as exc:
        raise ConversionFailedError(f"Chromium 渲染失败：{str(exc)[:200]}") from exc

    if not output.exists() or output.stat().st_size == 0:
        raise ConversionFailedError("渲染未产出有效 PDF，请检查 HTML 内容是否为空。")
    return output


def render_html_to_image(
    html_path: Path,
    workdir: Path,
    *,
    params: Any,
    full_page: bool = True,
    device_scale: float | None = None,
    timeout_ms: int = 60_000,
    ctx: JobContext | None = None,
) -> Path:
    """把 HTML 整页截图导出为 PNG / JPG。

    Args:
        html_path: HTML 文件路径。
        workdir: 输出目录。
        params: 转换参数（dpi → device_scale_factor，image_format，quality）。
        full_page: 是否截取整页（长页面会得到很长的图）。
        device_scale: 覆盖缩放因子；None 时由 dpi 推导。
        timeout_ms: 超时（毫秒）。
        ctx: 任务上下文。

    Returns:
        图片文件路径。

    Raises:
        EngineUnavailableError: Chromium 不可用。
        ConversionFailedError: 渲染失败。
    """
    _require_chromium()
    workdir.mkdir(parents=True, exist_ok=True)

    image_format = "jpeg" if str(getattr(params, "image_format", "png")).lower() in {"jpg", "jpeg"} else "png"
    suffix = "jpg" if image_format == "jpeg" else "png"
    output = workdir / f"output.{suffix}"

    # 96 DPI 是 CSS 像素的基准，因此 scale = dpi / 96
    scale = device_scale if device_scale is not None else max(0.5, min(4.0, params.effective_dpi() / 96.0))
    quality = int(min(100, max(40, getattr(params, "quality", 90))))

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page(device_scale_factor=scale)
                page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=timeout_ms)
                if ctx:
                    ctx.progress(50)
                page.emulate_media(media="screen")
                shoot_kwargs: dict[str, Any] = {
                    "path": str(output),
                    "full_page": full_page,
                    "type": image_format,
                }
                if image_format == "jpeg":
                    shoot_kwargs["quality"] = quality
                page.screenshot(**shoot_kwargs)
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise ConversionFailedError(f"页面截图失败：{str(exc)[:200]}") from exc

    if not output.exists():
        raise ConversionFailedError("截图未产出文件。")
    return output


def render_markdown_export(
    html_content: str,
    workdir: Path,
    *,
    params: Any | None = None,
    title: str = "document",
) -> Path:
    """把前端组装好的完整 HTML 渲染为 PDF（Markdown 导出通道）。

    Args:
        html_content: 完整 HTML 文本（前端已注入 KaTeX / Mermaid 渲染结果）。
        workdir: 输出目录。
        params: 转换参数；None 时使用默认 A4。
        title: 文档标题（写入 PDF 元数据）。

    Returns:
        PDF 文件路径。

    Raises:
        EngineUnavailableError: Chromium 不可用。
        ConversionFailedError: 渲染失败。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    html_path = workdir / "source.html"
    html_path.write_text(html_content, encoding="utf-8")

    from app.modules.converter.base import ConversionParams

    return render_html_to_pdf(
        html_path,
        workdir,
        params=params or ConversionParams(paper_size="A4", margin_mm=16.0),
    )


__all__ = [
    "PAPER_SIZES",
    "render_html_to_image",
    "render_html_to_pdf",
    "render_markdown_export",
]
