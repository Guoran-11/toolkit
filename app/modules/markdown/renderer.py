"""Markdown 导出通道的服务端渲染辅助。

职责：
    1. **安全净化** —— 在服务端对前端提交的 HTML 做一次独立过滤（防御纵深）。
       前端已用 DOMPurify，但服务端不能假设请求一定来自自家页面：
       导出接口是公开的 HTTP 端点，任何人都可以直接 POST 一段含 ``<script>`` 的 HTML。
       渲染进 PDF 本身危害有限，但同一份 HTML 也会被直接落盘为 .html 交付给用户，
       那才是真正的 XSS 载体（用户双击打开即触发）。
    2. **文档包装** —— 补全 ``<!DOCTYPE>``、``<meta charset>`` 与打印样式，
       让导出的 PDF 与 HTML 都保持自包含、可直接分发。

**为什么用「黑名单」而不是「白名单」**
    Markdown 导出接口的输入是前端 ``buildExportHtml()`` 生成的**完整文档**，
    其中包含大量精心构造的样式与 KaTeX / Mermaid 的 SVG 输出。
    若按标签白名单过滤，会连带剥掉 ``<style>``、``<head>``、``<meta>``
    以及 SVG 中的 ``linearGradient`` / ``foreignObject`` 等元素，直接破坏导出效果。
    因此这里采取「移除危险构造、保留其余结构」的保守策略，并额外做了三件事：
        · 剔除 ``<script>`` / ``<iframe>`` / ``<object>`` 等危险标签**及其内容**；
        · 剔除 ``on*`` 事件属性与 ``javascript:`` / 危险 ``data:`` 等 URL；
        · **对文本节点做 HTML 转义**（``HTMLParser`` 会把 ``&lt;script&gt;`` 还原成
          ``<script>``，不转义就等于亲手把标签注回文档）；
        · 丢弃 ``<meta http-equiv="refresh">``（无需 JavaScript 的导航型 XSS 通道）。
    这些正是 XSS 的主要载体。样式与结构得以保留。

    ``<style>`` 块会被单独做一次 CSS 级清洗（拦截 ``@import`` 与 ``expression()``），
    避免通过 CSS 引入外部资源或执行旧式脚本；其内容属于 CDATA，不做 HTML 转义。
"""

from __future__ import annotations

import logging
import re
from html import escape
from html.parser import HTMLParser
from typing import Final

logger = logging.getLogger(__name__)

# 危险标签：连同其内容一起丢弃
DROPPED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "script",
        "iframe",
        "object",
        "embed",
        "applet",
        "noscript",
        "template",
        "base",
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "link",  # 阻止通过 <link> 引入外部样式表
    }
)

# 自闭合元素集合（用于包装与闭合判断）
_VOID_TAGS: Final[frozenset[str]] = frozenset(
    {"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)

# 危险 URL 协议。
# 注意不能直接对原始值做前缀匹配：浏览器按 URL 规范会先剥离协议内部的
# 空白与控制字符，因此 "jav\tascript:" 等价于 "javascript:"，必须先归一化。
_URL_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x20\x7f]+")

# 可执行脚本的危险协议前缀（归一化后比对）
_DANGEROUS_SCHEMES: Final[tuple[str, ...]] = ("javascript:", "vbscript:", "livescript:", "mocha:")

# data: 协议中可执行脚本的媒体类型。
# 其余 data: 必须保留——前端 buildExportHtml() 会把图片与字体以 base64 data URI 内嵌。
_DANGEROUS_DATA_TYPES: Final[frozenset[str]] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
    }
)

# CSS 级危险构造
_DANGEROUS_CSS_RE: Final[re.Pattern[str]] = re.compile(
    # @import 由 ';' 终止，或直接延续到样式表结尾（两者在 CSS 中都是合法终止形式）
    r"@import[^;]*(?:;|$)|expression\s*\(|url\s*\(\s*['\"]?\s*(?:javascript|vbscript)\s*:",
    re.IGNORECASE,
)

# 需要检查 URL 安全的属性
_URL_ATTRS: Final[frozenset[str]] = frozenset({"href", "src", "xlink:href", "action", "formaction", "poster"})


def _is_dangerous_url(value: str) -> bool:
    """判断属性值是否为可执行脚本的危险 URL。

    Args:
        value: 属性值。

    Returns:
        危险返回 True。

    说明：
        · 先剥离浏览器会忽略的空白/控制字符，堵住 ``jav\\tascript:`` 这类绕过；
        · ``data:`` 只拦截可执行脚本的媒体类型（``text/html`` / ``xhtml`` /
          ``image/svg+xml``），图片与字体 data URI 照常放行。
    """
    probe = _URL_NOISE_RE.sub("", value or "").lower()
    if probe.startswith(_DANGEROUS_SCHEMES):
        return True
    if probe.startswith("data:"):
        media_type = probe[5:].split(",", 1)[0].split(";", 1)[0].strip()
        return media_type in _DANGEROUS_DATA_TYPES
    return False


class _DocumentSanitizer(HTMLParser):
    """保守式 HTML 净化器。

    输出时**保留标签的原始大小写**。这一点很关键：HTML 解析对 SVG 元素名大小写敏感，
    把 ``foreignObject`` 小写成 ``foreignobject`` 会让 KaTeX / Mermaid 的图形直接失效。
    """

    def __init__(self) -> None:
        """初始化。"""
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._drop_depth = 0
        # <style> 是 CDATA 元素，其文本是 CSS 而非 HTML，不能做 HTML 转义
        self._raw_text_depth = 0
        self.removed: dict[str, int] = {}

    # -------------------------------------------------- 内部

    @staticmethod
    def _is_refresh(attrs: list[tuple[str, str | None]]) -> bool:
        """判断 meta 标签是否为自动刷新/跳转。

        ``<meta http-equiv="refresh" content="0;url=data:text/html,...">`` 是
        一条不依赖 JavaScript 的导航 XSS 通道，导出产物不需要该能力。

        Args:
            attrs: 属性列表。

        Returns:
            是 ``http-equiv="refresh"`` 时返回 True。
        """
        return any(
            name.lower() == "http-equiv" and (value or "").strip().lower() == "refresh"
            for name, value in attrs
        )

    def _note(self, key: str) -> None:
        """记录被移除项的次数。

        Args:
            key: 记录键（标签名或属性名）。
        """
        self.removed[key] = self.removed.get(key, 0) + 1

    def _filter_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        """过滤属性。

        Args:
            tag: 所属标签名（保留原始大小写）。
            attrs: 属性列表。

        Returns:
            拼接好的属性串（含前导空格）。
        """
        parts: list[str] = []
        for name, value in attrs:
            lowered = name.lower()

            # 1) 事件处理器（onclick / onerror / onload ...）
            if lowered.startswith("on"):
                self._note(f"attr:{lowered}")
                continue

            # 2) srcdoc 可以直接注入完整 HTML 文档
            if lowered == "srcdoc":
                self._note("attr:srcdoc")
                continue

            # 3) 危险 URL 协议
            if lowered in _URL_ATTRS and value and _is_dangerous_url(value):
                self._note(f"attr:{lowered}:unsafe-url")
                continue

            if value is None:
                parts.append(f" {name}")
                continue
            parts.append(f' {name}="{escape(value, quote=True)}"')

        del tag  # 保留参数以便未来做标签级差异化策略
        return "".join(parts)

    # -------------------------------------------------- HTMLParser 回调

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """处理起始标签。

        Args:
            tag: 标签名（保留原始大小写）。
            attrs: 属性列表。
        """
        lowered = tag.lower()

        if lowered in DROPPED_TAGS:
            self._drop_depth += 1
            self._note(f"tag:{lowered}")
            return
        if self._drop_depth:
            return

        # 自动刷新/跳转：整条 meta 丢弃
        if lowered == "meta" and self._is_refresh(attrs):
            self._note("tag:meta:refresh")
            return

        if lowered == "style":
            self._raw_text_depth += 1

        self._out.append(f"<{tag}{self._filter_attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        """处理结束标签。

        Args:
            tag: 标签名。
        """
        lowered = tag.lower()
        if lowered in DROPPED_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return

        if lowered == "style" and self._raw_text_depth:
            self._raw_text_depth -= 1

        self._out.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """处理自闭合标签。

        Args:
            tag: 标签名。
            attrs: 属性列表。
        """
        lowered = tag.lower()
        if lowered in DROPPED_TAGS:
            self._note(f"tag:{lowered}")
            return
        if self._drop_depth:
            return
        if lowered == "meta" and self._is_refresh(attrs):
            self._note("tag:meta:refresh")
            return
        self._out.append(f"<{tag}{self._filter_attrs(tag, attrs)} />")

    def handle_data(self, data: str) -> None:
        """处理文本节点。

        **文本必须转义**。``HTMLParser(convert_charrefs=True)`` 会在回调之前把
        字符引用还原（``&lt;script&gt;`` → ``<script>``）。若原样写回输出，代码块里
        的转义文本就会变成真正的标签 —— 而前端 ``buildExportHtml()`` 正是用
        ``&lt;script&gt;`` 来编码代码块的，于是导出产物（用户双击即打开的 .html）
        被直接注入 XSS。因此这里对文本节点做 HTML 转义。

        ``<style>`` 属于 CDATA 元素，其内容由 :func:`_sanitize_style_blocks`
        单独做 CSS 级清洗，必须原样保留（转义会破坏 CSS 选择器中的 ``>``）。

        Args:
            data: 文本内容。
        """
        if self._drop_depth:
            return
        if self._raw_text_depth:
            self._out.append(data)
            return
        self._out.append(escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        """丢弃注释（条件注释等是历史 XSS 载体）。

        Args:
            data: 注释内容（未使用）。
        """
        del data
        self._note("comment")

    def result(self) -> str:
        """返回净化结果。

        Returns:
            净化后的 HTML 文本。
        """
        return "".join(self._out)


def sanitize_css(css: str) -> str:
    """清洗 CSS 文本。

    Args:
        css: 原始 CSS。

    Returns:
        移除危险构造后的 CSS。
    """
    return _DANGEROUS_CSS_RE.sub("", css or "")


def sanitize_document(html: str) -> tuple[str, dict[str, object]]:
    """净化完整 HTML 文档。

    Args:
        html: 原始 HTML（片段或完整文档）。

    Returns:
        ``(净化后的 HTML, 报告)``。报告中含被移除项统计，便于日志留痕。
    """
    parser = _DocumentSanitizer()
    try:
        parser.feed(html or "")
        parser.close()
        cleaned = parser.result()
    except Exception as exc:  # noqa: BLE001 - 解析异常时退化为纯文本，绝不原样输出
        logger.warning("HTML 净化失败，退化为纯文本输出：%s", exc)
        return f"<pre>{escape(html or '')}</pre>", {"fallback": True}

    # 二次处理 <style> 内容（HTMLParser 把 style 的文本当作 data，这里单独清洗）
    cleaned = _sanitize_style_blocks(cleaned)

    report: dict[str, object] = {
        "removed": dict(sorted(parser.removed.items())),
        "removed_total": sum(parser.removed.values()),
    }
    return cleaned, report


def _sanitize_style_blocks(html: str) -> str:
    """对 ``<style>`` 块内容做 CSS 级清洗。

    Args:
        html: HTML 文本。

    Returns:
        清洗后的 HTML。
    """

    def _replace(match: re.Match[str]) -> str:
        """处理单个 style 块。

        Args:
            match: 正则匹配。

        Returns:
            清洗后的 style 块。
        """
        opening, content, closing = match.group(1), match.group(2), match.group(3)
        return f"{opening}{sanitize_css(content)}{closing}"

    return re.sub(
        r"(<style[^>]*>)(.*?)(</style>)",
        _replace,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )


def extract_title(html: str, default: str = "document") -> str:
    """从 HTML 中提取标题。

    Args:
        html: HTML 文本。
        default: 提取不到时的兜底标题。

    Returns:
        标题字符串。
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        if title:
            return title

    match = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"<[^>]+>", "", match.group(1))
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return title

    return default


# 打印样式：背景色输出 + 分页控制（与转换模块的思路一致）
_PRINT_CSS: Final[str] = """
@page { margin: 16mm; }
@media print {
  *, *::before, *::after {
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }
  pre, blockquote, table, img, svg { break-inside: avoid; page-break-inside: avoid; }
  h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
}
"""

# 片段包装时使用的默认文档样式（前端未提供样式时的兜底）
_DOCUMENT_CSS: Final[str] = """
:root { color-scheme: light; }
html, body { margin: 0; padding: 0; }
body {
  max-width: 860px; margin: 0 auto; padding: 32px 24px 64px;
  font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 16px; line-height: 1.78; color: #1f2733; background: #fff;
  word-wrap: break-word;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.35; margin: 1.6em 0 0.6em; font-weight: 600; }
h1 { font-size: 1.85em; border-bottom: 1px solid #e5e7eb; padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #f3f4f6; padding-bottom: 0.25em; }
h3 { font-size: 1.25em; }
p { margin: 0.85em 0; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
code {
  font-family: ui-monospace, "Cascadia Mono", Consolas, "JetBrains Mono", monospace;
  font-size: 0.9em; background: #f4f6fa; padding: 0.15em 0.4em; border-radius: 4px;
}
pre {
  background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 14px 16px; overflow-x: auto; line-height: 1.6;
}
pre code { background: none; padding: 0; font-size: 0.88em; }
blockquote {
  margin: 1em 0; padding: 0.5em 1em; border-left: 4px solid #2563eb;
  background: #f8fafc; color: #4b5563;
}
table { border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: 0.94em; }
th, td { border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; vertical-align: top; }
th { background: #f3f4f6; font-weight: 600; }
img { max-width: 100%; height: auto; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 2em 0; }
ul, ol { padding-left: 1.6em; }
li { margin: 0.3em 0; }
ul.contains-task-list { list-style: none; padding-left: 0.6em; }
"""


def wrap_document(body_html: str, *, title: str) -> str:
    """把 HTML 片段包装为自包含的完整文档。

    Args:
        body_html: 正文 HTML 片段（应已净化）。
        title: 文档标题。

    Returns:
        完整 HTML 文本。
    """
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_DOCUMENT_CSS}</style>
<style>{_PRINT_CSS}</style>
</head>
<body>
{body_html}
</body>
</html>
"""


def ensure_safe_document(html: str, *, title: str) -> tuple[str, dict[str, object]]:
    """净化并确保为完整、可渲染的 HTML 文档。

    输入已是完整文档时保留其结构（含前端生成的 KaTeX / 高亮样式），
    只补一个缺失的 ``charset`` 声明 —— Chromium 渲染中文必须有它，
    否则可能按 Latin-1 解码而产生乱码；同时补齐缺失的 ``<!DOCTYPE>``，
    否则浏览器进入怪异模式（quirks mode），导出页面的排版与 PDF 都会走样。

    Args:
        html: 原始 HTML（片段或完整文档）。
        title: 兜底标题。

    Returns:
        ``(完整 HTML 文本, 净化报告)``。
    """
    if not (html or "").strip():
        raise ValueError("导出内容为空")

    cleaned, report = sanitize_document(html)
    lowered = cleaned.lower()

    if "<html" in lowered:
        # 完整文档：补齐 DOCTYPE、charset 与打印样式
        if not lowered.lstrip().startswith("<!doctype"):
            cleaned = "<!DOCTYPE html>\n" + cleaned
        if "<meta charset" not in lowered and "<meta http-equiv" not in lowered:
            cleaned = re.sub(
                r"(<head[^>]*>)",
                r'\1<meta charset="utf-8">',
                cleaned,
                count=1,
                flags=re.IGNORECASE,
            )
        # 确保打印样式存在（前端样式里没有分页控制）
        if "@page" not in cleaned:
            cleaned = re.sub(
                r"</head>",
                f"<style>{_PRINT_CSS}</style></head>",
                cleaned,
                count=1,
                flags=re.IGNORECASE,
            )
        return cleaned, report

    # 片段：包装为完整文档（打印样式已包含在 wrap_document 中）
    return wrap_document(cleaned, title=title), report


__all__ = [
    "DROPPED_TAGS",
    "ensure_safe_document",
    "extract_title",
    "sanitize_css",
    "sanitize_document",
    "wrap_document",
]
