"""标准库 HTTP 客户端（解析抓取 + 媒体流下载 + SSRF 校验入口）。

契约 §5.2 的落地：
    · 只用 ``urllib.request``，不引入 httpx/aiohttp/requests；
    · **手动**跟随重定向，每一跳都回调 ``registry.validate_hop`` 重新校验白名单与
      公网地址（默认的自动重定向会绕过校验，这是 SSRF 防护的关键点）；
    · 仅允许 https（``http://`` 归一化为 https，其它 scheme 直接拒绝）；
    · 固定 UA、``Accept-Encoding: identity``（避免 gzip 干扰 Range 分片与
      ``Content-Length`` 判定）；
    · 媒体下载返回可读流，不把大文件整体读入内存。

错误约定：本模块的传输层错误统一抛 :class:`NetError`（``OSError`` 子类，不直接暴露给
用户）。调用方（适配器 / 下载器）负责把它映射成 ``PARSE_FAILED`` / ``DOWNLOAD_FAILED``
等契约错误码——这样网络抖动不会被误报成「平台解析失败」或裸 500。
"""

from __future__ import annotations

import contextlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from app.core.errors import OutputTooLargeError, VideoParseError

#: 桌面端 UA（B站接口与分享页均接受）
UA_DESKTOP: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
#: 移动端 UA（抖音/快手分享页在移动 UA 下更容易直接返回内嵌 JSON）
UA_MOBILE: Final[str] = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

DEFAULT_TIMEOUT: Final[float] = 5.0
DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": UA_DESKTOP,
    "Accept": "*/*",
    "Accept-Encoding": "identity",
}

CHUNK_SIZE: Final[int] = 1024 * 1024
#: 解析用响应体上限（8 MB），防止异常平台返回超大页面把内存打满
_MAX_TEXT_BYTES: Final[int] = 8 * 1024 * 1024

_ContentRangeRe: Final[re.Pattern[str]] = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.IGNORECASE)
_CharsetRe: Final[re.Pattern[str]] = re.compile(r"charset=([A-Za-z0-9_\-]+)", re.IGNORECASE)

#: 校验器签名：输入 URL，返回归一化 URL（不合法时抛 ToolkitError 子类）
HopValidator = Callable[[str], str]


class NetError(OSError):
    """传输层错误（连接失败、超时、非 2xx、响应体畸形）。"""


# ---------------------------------------------------------------- 重定向控制


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 的自动重定向，改由上层逐跳校验后跟随。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """始终拒绝自动跟随（返回 None 表示交由错误处理器返回 3xx）。

        Args:
            req: 当前请求。
            fp: 响应流。
            code: 状态码。
            msg: 状态短语。
            headers: 响应头。
            newurl: 重定向目标。

        Returns:
            始终 None。
        """
        return None


_opener: Final[urllib.request.OpenerDirector] = urllib.request.build_opener(_NoRedirect)


# ---------------------------------------------------------------- 内部工具


def _merge_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """合并默认请求头与调用方覆盖项。

    Args:
        headers: 调用方额外/覆盖的请求头。

    Returns:
        合并后的请求头字典。
    """
    merged = dict(DEFAULT_HEADERS)
    if headers:
        for key, value in headers.items():
            if value is None:
                continue
            merged[str(key)] = str(value)
    return merged


def _headers_dict(headers: Any) -> dict[str, str]:
    """把 ``http.client.HTTPMessage`` 转成小写键的普通字典。

    Args:
        headers: urllib 的响应头对象。

    Returns:
        小写键字典（便于 ``headers.get("content-length")``）。
    """
    result: dict[str, str] = {}
    if headers is None:
        return result
    try:
        for key, value in headers.items():
            result[str(key).lower()] = str(value)
    except (AttributeError, TypeError):
        return result
    return result


def _read_limited(response: Any, limit: int = _MAX_TEXT_BYTES) -> bytes:
    """读取响应体（带上限）。

    Args:
        response: urllib 响应对象。
        limit: 最大读取字节数。

    Returns:
        响应体字节串（超限时截断）。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        block = response.read(64 * 1024)
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if total >= limit:
            break
    return b"".join(chunks)


def _decode(body: bytes, headers: dict[str, str]) -> str:
    """按响应头 charset 解码文本，兜底 utf-8。

    Args:
        body: 响应体。
        headers: 小写键响应头。

    Returns:
        解码后的文本。
    """
    charset = "utf-8"
    match = _CharsetRe.search(headers.get("content-type", ""))
    if match:
        candidate = match.group(1).lower()
        if candidate not in {"utf-8", "utf8", "gbk", "gb2312", "gb18030", "latin-1"}:
            candidate = "utf-8"
        charset = candidate
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _validate(url: str, validator: HopValidator | None, *, media: bool = False) -> str:
    """对单跳 URL 做校验与归一化。

    Args:
        url: 待校验 URL。
        validator: 显式校验器；None 时使用默认实现。
        media: 是否为媒体地址（默认校验器会区分平台白名单与公网地址）。

    Returns:
        归一化后的 URL。

    Raises:
        ToolkitError: 校验失败（由校验器抛出）。
    """
    if validator is not None:
        return validator(url)
    # 惰性导入：registry 在展开短链时也会调用本模块，顶层导入会形成循环依赖
    from app.modules.video.adapters import registry

    if media:
        return registry.validate_public_url(url)
    return registry.validate_hop(url)


def _run_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, dict[str, str], bytes]:
    """执行一次不跟随重定向的请求。

    Args:
        url: 目标 URL。
        method: HTTP 方法。
        headers: 请求头。
        data: 请求体。
        timeout: 超时秒数。

    Returns:
        ``(状态码, 小写键响应头, 响应体)``；3xx 也会正常返回（供上层逐跳处理）。

    Raises:
        NetError: 连接失败、超时或 DNS 失败。
    """
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in _merge_headers(headers).items():
        request.add_header(key, value)

    try:
        with _opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
            body = b"" if method.upper() == "HEAD" else _read_limited(response)
            return status, _headers_dict(response.headers), body
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        response_headers = _headers_dict(exc.headers)
        if 300 <= status < 400:
            return status, response_headers, b""
        body = b""
        with contextlib.suppress(Exception):
            body = _read_limited(exc)
        return status, response_headers, body
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NetError(f"网络请求失败：{reason}") from exc


def _follow(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = 5,
    validator: HopValidator | None = None,
    media: bool = False,
) -> tuple[str, int, dict[str, str], bytes]:
    """逐跳校验并跟随重定向。

    Args:
        url: 起始 URL。
        method: HTTP 方法。
        headers: 请求头。
        data: 请求体（POST 用；每一跳都会重新发送）。
        timeout: 单跳超时。
        max_redirects: 最大跳数。
        validator: 每跳校验器；None 时按 ``media`` 选择默认实现。
        media: 是否为媒体地址。

    Returns:
        ``(最终 URL, 状态码, 响应头, 响应体)``。

    Raises:
        NetError: 重定向次数超限或缺少 Location。
        ToolkitError: 某一跳校验失败。
    """
    current = _validate(url, validator, media=media)
    for _hop in range(max_redirects + 1):
        status, response_headers, body = _run_request(
            current, method=method, headers=headers, data=data, timeout=timeout
        )
        if 300 <= status < 400:
            location = response_headers.get("location", "").strip()
            if not location:
                raise NetError("重定向响应缺少 Location")
            current = _validate(urllib.parse.urljoin(current, location), validator, media=media)
            continue
        return current, status, response_headers, body
    raise NetError("重定向次数过多")


# ---------------------------------------------------------------- 文本 / JSON


def validate_media_url(url: str) -> str:
    """校验媒体直链（https + 公网地址，不要求属于平台白名单）。

    Args:
        url: 媒体地址。

    Returns:
        归一化后的 URL。

    Raises:
        UrlBlockedError: SSRF 校验失败。
    """
    return _validate(url, None, media=True)


def http_get_text(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_redirects: int = 5,
    validator: HopValidator | None = None,
) -> tuple[str, str, int]:
    """GET 一个页面/接口并返回文本。

    Args:
        url: 目标 URL。
        timeout: 单跳超时秒数。
        headers: 附加请求头（如 Referer / 移动端 UA）。
        max_redirects: 最大重定向跳数。
        validator: 每跳校验器（默认平台白名单校验）。

    Returns:
        ``(最终 URL, 文本, 状态码)``；非 2xx 时文本可能为空，由调用方判断状态码。

    Raises:
        NetError: 网络失败或重定向异常。
    """
    final_url, status, response_headers, body = _follow(
        url,
        timeout=timeout,
        headers=headers,
        max_redirects=max_redirects,
        validator=validator,
    )
    return final_url, _decode(body, response_headers), status


def http_get_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_redirects: int = 5,
    validator: HopValidator | None = None,
) -> tuple[str, Any, int]:
    """GET 一个 JSON 接口。

    Args:
        url: 目标 URL。
        timeout: 单跳超时秒数。
        headers: 附加请求头。
        max_redirects: 最大重定向跳数。
        validator: 每跳校验器。

    Returns:
        ``(最终 URL, 解析后的 JSON, 状态码)``；非 2xx 时 JSON 为 None。

    Raises:
        NetError: 网络失败。
        VideoParseError: 2xx 但响应不是合法 JSON。
    """
    final_url, text, status = http_get_text(
        url,
        timeout=timeout,
        headers=headers,
        max_redirects=max_redirects,
        validator=validator,
    )
    if not 200 <= status < 300:
        return final_url, None, status
    try:
        return final_url, json.loads(text), status
    except json.JSONDecodeError as exc:
        raise VideoParseError("平台接口返回了无法解析的数据（可能已调整接口）") from exc


def http_post_json(
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_redirects: int = 3,
    validator: HopValidator | None = None,
) -> tuple[str, Any, int, dict[str, str]]:
    """POST 一个 JSON 接口，并返回响应头（需要读取 ``Set-Cookie`` 时使用）。

    典型用途是抖音适配器向平台注册接口申请**匿名访客 ID**（``ttwid``）：该接口
    无需登录、无签名，响应体里的业务数据并不重要，关键是 ``Set-Cookie`` 响应头，
    因此本函数把最终 URL、解析后的 JSON、状态码与小写键响应头一起返回。

    SSRF 防护与 GET 完全一致：逐跳跟随重定向，**每一跳**都重新校验（默认
    ``registry.validate_public_url``，只要求公网可达、不要求属于平台白名单；
    调用方也可显式传入 ``validator`` 收窄范围）；仅允许 http(s)，``http://``
    归一化为 ``https://``。

    Args:
        url: 目标 URL。
        json_body: JSON 请求体；None 表示发送空体 POST。
        timeout: 单跳超时秒数。
        headers: 附加请求头（未显式给 ``Content-Type`` 时自动补
            ``application/json``）。
        max_redirects: 最大重定向跳数。
        validator: 每跳校验器；None 时使用公网校验（``validate_public_url``）。

    Returns:
        ``(最终 URL, 解析后的 JSON, 状态码, 小写键响应头)``；非 2xx 时 JSON 为 None
        （状态码与响应头仍然返回，便于调用方判断）。

    Raises:
        NetError: 网络失败或重定向异常。
        ToolkitError: SSRF 校验失败（由校验器抛出）。
        VideoParseError: 2xx 但响应不是合法 JSON。
    """
    body = b""
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    request_headers = dict(headers or {})
    if json_body is not None and not any(
        key.lower() == "content-type" for key in request_headers
    ):
        request_headers["Content-Type"] = "application/json"

    final_url, status, response_headers, raw = _follow(
        url,
        method="POST",
        headers=request_headers,
        data=body,
        timeout=timeout,
        max_redirects=max_redirects,
        validator=validator,
        # media=True 让默认校验器走 validate_public_url（公网即可），而不是平台白名单
        media=True,
    )
    if not 200 <= status < 300:
        return final_url, None, status, response_headers
    try:
        return final_url, json.loads(_decode(raw, response_headers)), status, response_headers
    except json.JSONDecodeError as exc:
        raise VideoParseError("平台接口返回了无法解析的数据（可能已调整接口）") from exc


def http_head(
    url: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    max_redirects: int = 3,
) -> tuple[int, dict[str, str]]:
    """发送 HEAD 请求并跟随重定向。

    Args:
        url: 媒体地址。
        timeout: 单跳超时。
        headers: 附加请求头。
        max_redirects: 最大跳数。

    Returns:
        ``(最终状态码, 小写键响应头)``。

    Raises:
        NetError: 网络失败。
    """
    _final, status, response_headers, _body = _follow(
        url,
        method="HEAD",
        timeout=timeout,
        headers=headers,
        max_redirects=max_redirects,
        media=True,
    )
    return status, response_headers


# ---------------------------------------------------------------- 媒体流


@dataclass(slots=True)
class MediaProbe:
    """媒体地址探测结果（HEAD / Range: 0-0）。

    Attributes:
        status: HTTP 状态码（0 表示探测失败）。
        total_size: 文件总字节数；未知为 None。
        accept_ranges: 是否支持 Range 分片。
        content_type: Content-Type。
        final_url: 重定向后的最终地址。
    """

    status: int = 0
    total_size: int | None = None
    accept_ranges: bool = False
    content_type: str | None = None
    final_url: str = ""


def open_stream(
    url: str,
    *,
    range_start: int | None = None,
    range_end: int | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> tuple[Any, int, dict[str, str]]:
    """打开媒体可读流（可选 Range）。

    返回的响应对象由调用方负责 ``close()``；本函数已跟随重定向并在每跳做公网校验。

    Args:
        url: 媒体地址。
        range_start: Range 起始字节（含）。
        range_end: Range 结束字节（含）；None 表示到文件末尾。
        timeout: 超时秒数。
        headers: 平台媒体请求头（如 B站 CDN 强制要求的 ``Referer``）。

    Returns:
        ``(响应对象, 状态码, 小写键响应头)``。

    Raises:
        NetError: 网络失败或重定向异常。
        ToolkitError: SSRF 校验失败。
    """
    range_headers: dict[str, str] = {}
    if range_start is not None:
        end_text = "" if range_end is None else str(range_end)
        range_headers["Range"] = f"bytes={range_start}-{end_text}"
    merged_headers = _merge_headers({**range_headers, **(headers or {})})

    request = urllib.request.Request(url, method="GET")
    for key, value in merged_headers.items():
        request.add_header(key, value)

    current = _validate(url, None, media=True)
    for _hop in range(4):
        try:
            response = _opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                location = _headers_dict(exc.headers).get("location", "")
                if not location:
                    raise NetError("媒体重定向缺少 Location") from exc
                current = _validate(urllib.parse.urljoin(current, location), None, media=True)
                request = urllib.request.Request(current, method="GET")
                for key, value in merged_headers.items():
                    request.add_header(key, value)
                continue
            status = int(exc.code)
            with contextlib.suppress(OSError):
                exc.close()
            return exc, status, _headers_dict(exc.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetError(f"媒体请求失败：{reason}") from exc
        status = int(getattr(response, "status", 0) or response.getcode())
        return response, status, _headers_dict(response.headers)
    raise NetError("媒体重定向次数过多")


def download_range(
    url: str,
    dest: Any,
    *,
    start: int | None = None,
    end: int | None = None,
    timeout: float = 30.0,
    chunk_size: int = CHUNK_SIZE,
    max_bytes: int | None = None,
    on_chunk: Callable[[int], None] | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    """下载一段媒体数据到文件。

    Args:
        url: 媒体地址。
        dest: 目标路径（``str`` / ``Path``）。
        start: Range 起始字节；None 表示整文件。
        end: Range 结束字节；None 表示到末尾。
        timeout: 单次连接超时。
        chunk_size: 每次读取块大小。
        max_bytes: 允许的最大累计字节数；超出抛 ``OutputTooLargeError``。
        on_chunk: 每写入一块后的回调（参数为本次写入字节数）。
        headers: 平台媒体请求头（B站 CDN 强制校验 ``Referer`` + UA）。

    Returns:
        实际写入的字节数。

    Raises:
        OutputTooLargeError: 超过 ``max_bytes``。
        NetError: 网络失败或服务端返回错误状态。
    """
    from pathlib import Path

    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    response, status, _headers = open_stream(
        url, range_start=start, range_end=end, timeout=timeout, headers=headers
    )
    written = 0
    try:
        if status >= 400:
            raise NetError(f"媒体服务器返回 HTTP {status}")
        with target.open("wb") as fp:
            while True:
                block = response.read(chunk_size)
                if not block:
                    break
                written += len(block)
                if max_bytes is not None and written > max_bytes:
                    raise OutputTooLargeError("文件超过单文件大小上限，已停止下载")
                fp.write(block)
                if on_chunk is not None:
                    on_chunk(len(block))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        if isinstance(exc, NetError):
            raise
        raise NetError(f"媒体下载中断：{exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            response.close()
    return written


def probe_media(
    url: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> MediaProbe:
    """探测媒体地址的总大小与 Range 支持情况。

    先发 HEAD；部分 CDN 对 HEAD 返回 403/405 或不带 ``Content-Length``，此时退化为
    ``GET Range: bytes=0-0`` 并从 ``Content-Range`` 里解析总大小。

    Args:
        url: 媒体地址。
        timeout: 超时秒数。
        headers: 平台媒体请求头（B站 CDN 强制校验 ``Referer`` + UA）。

    Returns:
        MediaProbe；完全探测失败时 ``status=0``、``total_size=None``，调用方退化为单流。
    """
    try:
        status, response_headers = http_head(url, timeout=timeout, headers=headers)
    except (NetError, OSError):
        status, response_headers = 0, {}

    total = _int_or_none(response_headers.get("content-length"))
    accept_ranges = "bytes" in response_headers.get("accept-ranges", "").lower()
    content_type = response_headers.get("content-type")

    if status < 400 and total is not None:
        return MediaProbe(
            status=status,
            total_size=total,
            accept_ranges=accept_ranges,
            content_type=content_type,
            final_url=url,
        )

    # 退化探测：Range 0-0
    response = None
    try:
        response, range_status, range_headers = open_stream(
            url, range_start=0, range_end=0, timeout=timeout, headers=headers
        )
        if range_status >= 400:
            return MediaProbe(status=range_status, content_type=content_type, final_url=url)
        match = _ContentRangeRe.search(range_headers.get("content-range", ""))
        if match:
            total = int(match.group(1))
        elif range_status == 200:
            total = _int_or_none(range_headers.get("content-length"))
        return MediaProbe(
            status=range_status,
            total_size=total,
            accept_ranges=range_status == 206 or accept_ranges,
            content_type=range_headers.get("content-type") or content_type,
            final_url=url,
        )
    except (NetError, OSError):
        return MediaProbe(status=0, content_type=content_type, final_url=url)
    finally:
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()


def _int_or_none(value: str | None) -> int | None:
    """把响应头里的整数字符串转 int。

    Args:
        value: 原始字符串。

    Returns:
        整数；无法解析返回 None。
    """
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "DEFAULT_HEADERS",
    "DEFAULT_TIMEOUT",
    "UA_DESKTOP",
    "UA_MOBILE",
    "MediaProbe",
    "NetError",
    "download_range",
    "http_get_json",
    "http_get_text",
    "http_head",
    "http_post_json",
    "open_stream",
    "probe_media",
    "validate_media_url",
]
