"""适配器公共契约：清晰度归一化、默认清晰度选择、解析上下文与通用解析工具。

契约 §4.2 的归一化表：
    ≥2160→4K；≥1440→2K；=1080 且 fps≥50→1080P60；=1080→1080P；=720→720P；
    =540→540P；=480→480P；<480→360P；无视频轨→audio。

默认选择策略：用户偏好可用则用；否则取 ≤1080P 的最高可用；**绝不默认 4K**
（体积大且多为会员专属），也不默认选择需登录/会员的清晰度。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.modules.video.models import VideoFormat, VideoItem

#: 清晰度从低到高（audio 单独处理，不参与视频排序）
_QUALITY_RANK: dict[str, int] = {
    "360P": 0,
    "480P": 1,
    "540P": 2,
    "720P": 3,
    "1080P": 4,
    "1080P60": 5,
    "2K": 6,
    "4K": 7,
}

#: 各归一化等级对应的近似高度（用于按高度筛选「≤1080P」）
_QUALITY_HEIGHT: dict[str, int] = {
    "360P": 360,
    "480P": 480,
    "540P": 540,
    "720P": 720,
    "1080P": 1080,
    "1080P60": 1080,
    "2K": 1440,
    "4K": 2160,
}


def normalize_quality(height: int | None, fps: int | None, has_video: bool = True) -> str:
    """把平台原始分辨率/帧率归一化为统一清晰度等级。

    Args:
        height: 视频高度（像素）；None 表示未知。
        fps: 帧率；None 视为未知。
        has_video: 是否为视频轨（False 直接返回 ``audio``）。

    Returns:
        ``4K`` / ``2K`` / ``1080P60`` / ``1080P`` / ``720P`` / ``540P`` / ``480P`` /
        ``360P`` / ``audio`` 之一。
    """
    if not has_video:
        return "audio"
    if not height or height <= 0:
        # 高度未知时给一个保守等级，避免把音频/畸形流标成高清
        return "360P"
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K"
    if height >= 1080:
        return "1080P60" if (fps or 0) >= 50 else "1080P"
    if height >= 720:
        return "720P"
    if height >= 540:
        return "540P"
    if height >= 480:
        return "480P"
    return "360P"


def _as_dict(fmt: VideoFormat | dict[str, Any]) -> dict[str, Any]:
    """把清晰度统一成字典（保留内部字段）。

    Args:
        fmt: VideoFormat 或原始字典。

    Returns:
        字典副本。
    """
    if isinstance(fmt, VideoFormat):
        data = fmt.to_api()
        data["url"] = fmt.url
        data["audio_url"] = fmt.audio_url
        return data
    return dict(fmt)


def _height_of(fmt: dict[str, Any]) -> int:
    """取清晰度字典的高度，缺失时按等级近似。

    Args:
        fmt: 清晰度字典。

    Returns:
        近似高度（像素）。
    """
    height = fmt.get("height")
    if isinstance(height, int) and height > 0:
        return height
    return _QUALITY_HEIGHT.get(str(fmt.get("quality") or ""), 0)


def pick_default_format(
    formats: Sequence[VideoFormat | dict[str, Any]],
    prefer_quality: str | None,
    allow_login: bool = False,
) -> dict[str, Any] | None:
    """按默认策略选择清晰度。

    Args:
        formats: 清晰度列表（VideoFormat 或 to_api 字典）。
        prefer_quality: 用户偏好等级（4K/1080P/720P/480P 等）。
        allow_login: 是否允许选中需要登录态的清晰度。

    Returns:
        选中的清晰度字典（契约字段 + ``url``/``audio_url`` 内部字段）；
        无可用项时返回 None。
    """
    candidates: list[dict[str, Any]] = []
    for item in formats:
        data = _as_dict(item)
        if data.get("need_vip"):
            # 会员专属清晰度只展示、不下载（合规红线）
            continue
        if data.get("need_login") and not allow_login:
            continue
        # 「显式无地址」的项（平台只给了档位、没给流）一律不可默认选中。
        # 注意：纯 to_api 字典没有 url 键，视为可用性未知，不能据此排除，
        # 否则会破坏只传契约字段的调用方与既有测试；但显式 available=False 要排除。
        if data.get("available") is False:
            continue
        if "url" in data and not data["url"]:
            continue
        candidates.append(data)

    video = [item for item in candidates if item.get("quality") != "audio"]
    if not video:
        # 音频分支与视频同源：上面的过滤（会员/登录/显式无地址）已一并生效
        audio = [item for item in candidates if item.get("quality") == "audio"]
        if not audio:
            return None
        audio.sort(key=lambda item: int(item.get("bitrate") or 0), reverse=True)
        return audio[0]

    if prefer_quality:
        exact = [item for item in video if item.get("quality") == prefer_quality]
        if exact:
            exact.sort(key=lambda item: int(item.get("bitrate") or 0), reverse=True)
            return exact[0]
        # 偏好「1080P」时，1080P60 也是同高度的合理命中
        if prefer_quality == "1080P":
            same_height = [item for item in video if item.get("quality") == "1080P60"]
            if same_height:
                same_height.sort(key=lambda item: int(item.get("bitrate") or 0), reverse=True)
                return same_height[0]

    at_most_1080 = [item for item in video if _height_of(item) <= 1080]
    pool = at_most_1080 or video
    reverse = bool(at_most_1080)
    pool.sort(
        key=lambda item: (
            _height_of(item),
            _QUALITY_RANK.get(str(item.get("quality") or ""), 0),
            int(item.get("fps") or 0),
            int(item.get("bitrate") or 0),
        ),
        reverse=reverse,
    )
    if at_most_1080:
        return pool[0]
    # 只有 4K/2K 可选时，退而取其中最低的一档（仍好过无产物；绝不默认 4K）
    return pool[0]


def quality_is_available(formats: Sequence[VideoFormat | dict[str, Any]], quality: str) -> bool:
    """判断某个清晰度等级是否存在可用（非会员、有地址）的项。

    Args:
        formats: 清晰度列表。
        quality: 归一化等级。

    Returns:
        是否存在。
    """
    for item in formats:
        data = _as_dict(item)
        if data.get("quality") != quality or data.get("need_vip"):
            continue
        if data.get("url"):
            return True
    return False


@dataclass(slots=True)
class ParseContext:
    """一次解析请求的上下文。

    Attributes:
        platform: 平台标识。
        cookie: 该平台的明文 Cookie（仅用于请求头，不得写入日志）。
        prefer_quality: 用户偏好清晰度。
        timeout: 单次 HTTP 超时秒数。
        mobile: 是否使用移动端 UA（抖音/快手分享页）。
        fetcher: 可注入的取数函数（供测试离线替换网络层）。
    """

    platform: str = ""
    cookie: str | None = None
    prefer_quality: str = "1080P"
    timeout: float = 5.0
    mobile: bool = False
    fetcher: Any = field(default=None)


class PlatformAdapter(Protocol):
    """平台适配器协议（契约 §4.3 的内部契约）。"""

    name: str
    hosts: tuple[str, ...]

    def match(self, url: str) -> bool:
        """判断 URL 是否属于本平台。

        Args:
            url: 目标 URL。

        Returns:
            是否匹配。
        """
        ...

    def normalize(self, url: str) -> str:
        """归一化 URL（短链展开、去跟踪参数）。

        Args:
            url: 原始 URL。

        Returns:
            归一化后的长链。
        """
        ...

    def parse(self, url: str, ctx: ParseContext) -> VideoItem:
        """抓取并解析作品元数据。

        Args:
            url: 归一化后的 URL。
            ctx: 解析上下文。

        Returns:
            作品元数据。

        Raises:
            ToolkitError: 解析失败（带明确错误码）。
        """
        ...


# ---------------------------------------------------------------- 通用解析工具


def to_iso(epoch: float | int | None) -> str | None:
    """Unix 时间戳 → 与 ``utc_now_iso()`` 同格式的 ISO 字符串。

    Args:
        epoch: 秒级时间戳（部分平台给毫秒，调用方先自行换算）。

    Returns:
        ``2026-10-12T09:30:00.000Z``；无效输入返回 None。
    """
    if not epoch:
        return None
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    if value > 1e12:  # 毫秒时间戳
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def as_int(value: Any, default: int | None = None) -> int | None:
    """尽力把任意值转为非负整数。

    注意：本函数**只用于尺寸/时长/计数**这类非负字段。业务错误码（如 B站 ``code=-404``）
    可能是负数，必须用 :func:`as_signed_int`——否则 -404 会被静默归一成 0，错误码映射
    全部失效（历史坑，见 qa 回归报告）。

    Args:
        value: 原始值（数字/字符串）。
        default: 无法转换时或为负数时的返回值。

    Returns:
        非负整数或 ``default``。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def as_signed_int(value: Any, default: int = 0) -> int:
    """把任意值转为可为负的整数（用于平台业务错误码）。

    Args:
        value: 原始值（数字/字符串）。
        default: 无法转换时的返回值。

    Returns:
        整数或 ``default``。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_text(value: Any, limit: int = 2000) -> str:
    """把任意值安全转为去空白文本。

    Args:
        value: 原始值。
        limit: 最大长度。

    Returns:
        文本；无效输入返回空串。
    """
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def first_url(value: Any) -> str | None:
    """从平台常见的 URL 容器结构中取第一个可用地址。

    兼容形态：字符串、``{"url_list": [...]}``、``{"url": ...}``、列表、
    ``{"Url": ...}``（快手 Apollo 缓存用大写键）。

    Args:
        value: 原始结构。

    Returns:
        URL 字符串；找不到返回 None。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = first_url(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "srcNoMark", "playUrl", "url", "Url", "src"):
            if key in value:
                found = first_url(value[key])
                if found:
                    return found
        return None
    return None


_SCRIPT_JSON_MARKERS: tuple[str, ...] = (
    "window._ROUTER_DATA",
    "window.RENDER_DATA",
    "window.__INITIAL_STATE__",
    "window.__APOLLO_STATE__",
    "window.__NUXT__",
)


def extract_script_json(html: str) -> dict[str, Any] | None:
    """从分享页 HTML 中提取 ``window.__INITIAL_STATE__`` 之类的内嵌 JSON。

    支持两种形态：
        1. 直接对象：``window.__APOLLO_STATE__ = {...}``；
        2. URL 编码的 JSON 字符串：``window.RENDER_DATA = "%7B...%7D"``（抖音分享页
           常见），先 ``unquote`` 再解析。

    实现方式：正则定位赋值标记，对象形态用括号配对扫描出完整对象（字符串内的括号与
    转义不参与计数）；字符串形态先按 JS 字符串字面量取出再解码。这样比「匹配到下一个
    分号」更稳，平台脚本里常出现对象内包含 ``;`` 的情况。

    Args:
        html: 页面 HTML。

    Returns:
        解析出的字典；未找到或解析失败返回 None。
    """
    for marker in _SCRIPT_JSON_MARKERS:
        index = html.find(marker)
        if index < 0:
            continue
        assign = html.find("=", index + len(marker))
        if assign < 0:
            continue
        cursor = assign + 1
        while cursor < len(html) and html[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= len(html):
            continue

        parsed: Any = None
        if html[cursor] == "{":
            snippet = _balanced_object(html, cursor)
            if snippet:
                parsed = _loads_tolerant(snippet)
        elif html[cursor] in {'"', "'"}:
            literal = _js_string_literal(html, cursor)
            if literal is not None:
                # RENDER_DATA 等字段是 encodeURIComponent 后的 JSON
                parsed = _loads_tolerant(urllib.parse.unquote(literal))
        if isinstance(parsed, dict):
            return parsed
    return None


def _js_string_literal(text: str, start: int) -> str | None:
    """取出 JS 字符串字面量的内容（处理转义）。

    Args:
        text: 页面文本。
        start: 起始引号下标。

    Returns:
        字符串内容；引号不配对返回 None。
    """
    quote = text[start]
    escaped = False
    buffer: list[str] = []
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            buffer.append("\\")
            buffer.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            raw = "".join(buffer)
            try:
                # 用 JSON 规则还原 \n / \" / \\ 等转义
                return str(json.loads(f'"{raw}"'))
            except json.JSONDecodeError:
                return raw
        buffer.append(char)
    return None


def _balanced_object(text: str, start: int) -> str | None:
    """从 ``start`` 处的 ``{`` 起扫描配对的完整对象文本。

    Args:
        text: 页面文本。
        start: 起始花括号下标。

    Returns:
        完整对象文本；括号不配对返回 None。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _loads_tolerant(snippet: str) -> Any | None:
    """容错解析 JSON（处理平台脚本里的 ``undefined`` / 单引号等非标准写法）。

    Args:
        snippet: JSON 文本片段。

    Returns:
        解析结果；失败返回 None。
    """
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        pass
    # 平台脚本里常见 `undefined`（JS 字面量），JSON 不认；先替换再试一次
    repaired = re.sub(r"\bundefined\b", "null", snippet)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def iter_dicts(value: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """深度优先遍历嵌套结构中的所有字典（限制深度，防畸形数据递归爆栈）。

    Args:
        value: 任意 JSON 结构。
        depth: 当前深度（内部递归用）。

    Yields:
        依次产出的字典。
    """
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from iter_dicts(child, depth + 1)


def find_dict_with(value: Any, *keys: str) -> dict[str, Any] | None:
    """在嵌套结构中查找同时包含指定键的字典。

    Args:
        value: 任意 JSON 结构。
        *keys: 必须同时存在的键。

    Returns:
        首个命中的字典；未找到返回 None。
    """
    for candidate in iter_dicts(value):
        if all(key in candidate for key in keys):
            return candidate
    return None


_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?P<name>[^\"']+)[\"'][^>]+content=[\"'](?P<content>[^\"']*)[\"']",
    re.IGNORECASE,
)


def parse_meta_tags(html: str) -> dict[str, str]:
    """提取页面 ``og:*`` / ``name`` 元信息。

    Args:
        html: 页面 HTML。

    Returns:
        ``属性名 -> 内容`` 的字典（键为小写）。
    """
    result: dict[str, str] = {}
    for match in _META_RE.finditer(html):
        name = match.group("name").strip().lower()
        content = match.group("content").strip()
        if name and content and name not in result:
            result[name] = content
    return result


__all__ = [
    "ParseContext",
    "PlatformAdapter",
    "as_int",
    "as_text",
    "extract_script_json",
    "find_dict_with",
    "first_url",
    "iter_dicts",
    "normalize_quality",
    "parse_meta_tags",
    "pick_default_format",
    "quality_is_available",
    "to_iso",
]
