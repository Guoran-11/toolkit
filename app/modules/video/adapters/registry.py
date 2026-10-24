"""平台注册表：域名白名单、链接提取、SSRF 校验与短链展开。

契约 §4.3 / §5.2：
    · 域名白名单是**唯一可信来源**，硬编码在本文件；
    · 短链逐跳校验，最多 5 跳，每跳重新校验 host 是否在白名单；
    · SSRF：只允许 https、拒绝字面内网/回环/链路本地/元数据地址，DNS 解析结果必须
      全部为公网地址；解析失败时仅**白名单平台域名**放行，其余一律拒绝（fail closed）。

安全取舍说明：白名单域名在 DNS 解析失败时仍放行，是为了兼容「离线包首次启动、
DNS 尚未就绪」以及测试环境；但字面 IP 与解析成功后的内网地址一律拒绝，因此
不会把本机或云元数据地址暴露给上游页面。
"""

from __future__ import annotations

import importlib
import ipaddress
import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, Final

from app.core.config import get_settings
from app.core.errors import PlatformUnsupportedError, ShortlinkError, UrlBlockedError
from app.modules.video import net

#: 平台 → 允许的 host（含短链域）。顺序即前端平台列表顺序。
PLATFORMS: Final[dict[str, list[str]]] = {
    "douyin": [
        "douyin.com",
        "www.douyin.com",
        "v.douyin.com",
        "iesdouyin.com",
        "www.iesdouyin.com",
    ],
    "bilibili": [
        "bilibili.com",
        "www.bilibili.com",
        "m.bilibili.com",
        "b23.tv",
    ],
    "kuaishou": [
        "kuaishou.com",
        "www.kuaishou.com",
        "v.kuaishou.com",
        "m.kuaishou.com",
    ],
}

#: 平台展示名
PLATFORM_LABELS: Final[dict[str, str]] = {
    "douyin": "抖音",
    "bilibili": "B站",
    "kuaishou": "快手",
}

#: 平台能力矩阵（契约 §3.1）
PLATFORM_CAPABILITIES: Final[dict[str, dict[str, bool]]] = {
    "douyin": {"video": True, "image": True, "multi_part": False},
    "bilibili": {"video": True, "image": False, "multi_part": True},
    "kuaishou": {"video": True, "image": True, "multi_part": False},
}

#: 媒体下载所需的平台请求头。
#:
#: 实测（2026-10-20，生产服务器）：B站媒体 CDN（bilivideo.com / upos-*.bilivideo.com）
#: 对媒体地址强制校验 ``Referer`` 与 UA —— 两者缺一即返回 403（单独补 UA 或单独补
#: Referer 都不行，必须同时带上）。抖音/快手 CDN 同样有防盗链，按各自站点补 Referer。
#: 这些是**平台公开页面本身就会发送的头**，不涉及伪造设备指纹或绕过签名。
MEDIA_HEADERS: Final[dict[str, dict[str, str]]] = {
    "bilibili": {"Referer": "https://www.bilibili.com/", "User-Agent": net.UA_DESKTOP},
    "douyin": {"Referer": "https://www.douyin.com/", "User-Agent": net.UA_DESKTOP},
    "kuaishou": {"Referer": "https://www.kuaishou.com/", "User-Agent": net.UA_DESKTOP},
}


def media_headers(platform: str | None) -> dict[str, str]:
    """取某平台媒体下载所需的请求头。

    Args:
        platform: 平台标识；None/未知平台返回空字典。

    Returns:
        可直接传给 ``net.download_range`` / ``net.probe_media`` 的请求头字典（副本）。
    """
    return dict(MEDIA_HEADERS.get(platform or "", {}))

#: 短链域名（需要先展开）
SHORTLINK_HOSTS: Final[frozenset[str]] = frozenset(
    {"v.douyin.com", "b23.tv", "v.kuaishou.com", "www.iesdouyin.com"}
)

#: 明确禁止访问的特殊主机名
_BLOCKED_HOSTS: Final[frozenset[str]] = frozenset(
    {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata"}
)
_BLOCKED_SUFFIXES: Final[tuple[str, ...]] = (".local", ".internal", ".localhost", ".localdomain")

#: 链接提取正则（契约 §3.2）：排除空白、引号、尖括号与中文标点
_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s\"'<>（）()，。！？【】《》〈〉「」\u3000]+",
    re.IGNORECASE,
)
_TRAILING_PUNCT: Final[str] = ".,;:!?、。，；：！？…·'\"）)】》」』"

#: DNS 缓存：(过期时间, 解析结果)；解析失败也缓存，避免反复阻塞
_DNS_TTL_SEC: Final[float] = 120.0
_dns_cache: dict[str, tuple[float, list[str] | None]] = {}
_dns_lock = threading.Lock()

#: 适配器实例缓存（避免每次请求重复 import / 实例化）
_adapters: dict[str, Any] = {}
_adapter_lock = threading.Lock()

_ADAPTER_MODULES: Final[dict[str, str]] = {
    "douyin": "app.modules.video.adapters.douyin",
    "bilibili": "app.modules.video.adapters.bilibili",
    "kuaishou": "app.modules.video.adapters.kuaishou",
}


# ---------------------------------------------------------------- 域名与链接


def _host_of(url: str) -> str:
    """取 URL 的小写主机名（去端口、去尾部点）。

    Args:
        url: 目标 URL。

    Returns:
        主机名；非法 URL 返回空串。
    """
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return ""
    host = parsed.hostname or ""
    return host.lower().rstrip(".")


def is_platform_host(host: str) -> bool:
    """判断 host 是否属于任一平台的允许域名（支持子域）。

    Args:
        host: 主机名。

    Returns:
        是否属于白名单。
    """
    target = (host or "").lower().rstrip(".")
    if not target:
        return False
    for hosts in PLATFORMS.values():
        for entry in hosts:
            if target == entry or target.endswith("." + entry):
                return True
    return False


def platform_for_url(url: str) -> str | None:
    """按域名判定 URL 所属平台。

    Args:
        url: 目标 URL（可含短链域）。

    Returns:
        平台标识；不属于任何平台返回 None。
    """
    host = _host_of(url)
    if not host:
        return None
    for platform, hosts in PLATFORMS.items():
        for entry in hosts:
            if host == entry or host.endswith("." + entry):
                return platform
    return None


def extract_links(text: str, limit: int = 20) -> list[str]:
    """从任意文本（分享文案 / 多行输入）中提取作品链接。

    规则：``http(s)://`` 开头、去掉尾部中文/英文标点、``http`` 归一化为 ``https``、
    同批按 URL 去重（保持出现顺序）、最多保留 ``limit`` 条。

    Args:
        text: 用户输入文本。
        limit: 最多返回条数（≤0 时按 1 处理）。

    Returns:
        去重后的链接列表。
    """
    cap = max(1, int(limit))
    result: list[str] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(_TRAILING_PUNCT)
        if not candidate:
            continue
        if candidate.lower().startswith("http://"):
            candidate = "https://" + candidate[7:]
        key = candidate.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) >= cap:
            break
    return result


# ---------------------------------------------------------------- SSRF 校验


def _is_public_ip(ip_text: str) -> bool:
    """判断 IP 是否为公网地址。

    Args:
        ip_text: IP 字符串。

    Returns:
        公网地址返回 True；内网/回环/链路本地/保留/组播地址返回 False。
    """
    try:
        address = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要按映射后的 IPv4 再判一次
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_public_ip(str(address.ipv4_mapped))
    return True


def _resolve_host(host: str) -> list[str] | None:
    """解析域名（带 TTL 缓存）。

    Args:
        host: 主机名。

    Returns:
        去重后的 IP 列表；解析失败返回 None。
    """
    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(host)
        if cached and cached[0] > now:
            return cached[1]

    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = sorted({str(info[4][0]) for info in infos if info[4]})
    except (socket.gaierror, OSError):
        ips = None

    with _dns_lock:
        _dns_cache[host] = (now + _DNS_TTL_SEC, ips)
    return ips


def _assert_public_host(host: str) -> None:
    """校验主机名解析结果全部为公网地址。

    Args:
        host: 主机名。

    Raises:
        UrlBlockedError: 命中内网/回环/元数据地址。
    """
    if not host:
        raise UrlBlockedError("链接缺少有效域名")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise UrlBlockedError("该链接指向本机或内网地址，已拒绝访问")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # 普通域名：必须能解析；白名单平台域名允许解析失败（离线场景）
        ips = _resolve_host(host)
        if ips is None:
            if is_platform_host(host):
                return
            raise UrlBlockedError("无法解析该域名，已拒绝访问") from None
        if not ips:
            raise UrlBlockedError("域名未解析到任何地址，已拒绝访问") from None
        for ip_text in ips:
            if not _is_public_ip(ip_text):
                raise UrlBlockedError("该链接解析到内网或保留地址，已拒绝访问") from None
        return

    if not _is_public_ip(host):
        raise UrlBlockedError("该链接指向内网或保留地址，已拒绝访问")


def _normalize_https_url(url: str) -> str:
    """校验 scheme 并把 ``http`` 归一化为 ``https``。

    Args:
        url: 原始 URL。

    Returns:
        归一化后的 URL。

    Raises:
        UrlBlockedError: scheme 非法或缺少主机。
    """
    raw = (url or "").strip()
    if not raw:
        raise UrlBlockedError("链接为空")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise UrlBlockedError("链接格式非法") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UrlBlockedError("仅允许访问 http(s) 链接")
    if not parsed.hostname:
        raise UrlBlockedError("链接缺少有效域名")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(("https", parsed.netloc, path, parsed.query, ""))


def validate_public_url(url: str) -> str:
    """校验并归一化一个可公开访问的 URL（媒体直链 / 解析目标）。

    Args:
        url: 待校验 URL。

    Returns:
        归一化后的 https URL。

    Raises:
        UrlBlockedError: scheme 非法、指向内网/回环/元数据地址、或域名无法解析。
    """
    normalized = _normalize_https_url(url)
    host = _host_of(normalized)
    _assert_public_host(host)
    return normalized


def validate_hop(url: str) -> str:
    """校验重定向链中的单跳 URL（要求属于平台白名单）。

    Args:
        url: 跳转目标 URL。

    Returns:
        归一化后的 URL。

    Raises:
        PlatformUnsupportedError: host 不在三平台白名单。
        UrlBlockedError: SSRF 校验失败。
    """
    normalized = validate_public_url(url)
    host = _host_of(normalized)
    if not is_platform_host(host):
        raise PlatformUnsupportedError("暂不支持该平台，当前支持抖音、B站、快手")
    return normalized


# ---------------------------------------------------------------- 短链展开


def _shortlink_hop(url: str) -> str:
    """短链展开过程中的单跳校验（失败统一转 ``ShortlinkError``）。

    Args:
        url: 跳转目标 URL。

    Returns:
        归一化 URL。

    Raises:
        ShortlinkError: 目标不是白名单平台或 SSRF 校验失败。
    """
    try:
        return validate_hop(url)
    except (UrlBlockedError, PlatformUnsupportedError) as exc:
        raise ShortlinkError("短链已失效或跳转到了不受支持的地址") from exc


def expand_shortlink(url: str, fetcher: Callable[[str], str | None] | None = None) -> str:
    """展开短链（最多 5 跳，每跳校验）。

    Args:
        url: 短链 URL。
        fetcher: 可注入的「取下一跳」函数（测试离线替换网络）；返回 None 表示
            不再跳转。生产环境为 None，走真实 HTTP。

    Returns:
        展开后的长链；本来就不是短链/无需跳转时返回归一化后的原 URL。

    Raises:
        ShortlinkError: 超过 5 跳、跳转断裂或目标不在白名单。
    """
    if fetcher is not None:
        current = _shortlink_hop(url)
        for _hop in range(5):
            try:
                next_url = fetcher(current)
            except ShortlinkError:
                raise
            except Exception as exc:
                raise ShortlinkError("短链展开失败，请重新复制链接") from exc
            if not next_url:
                return current
            current = _shortlink_hop(next_url)
        raise ShortlinkError("短链跳转次数过多（超过 5 跳），请重新复制链接")

    last_error: Exception | None = None
    # 契约 §4.3：短链展开超时 5 秒，失败重试 1 次（退避 1 秒）
    for attempt in range(2):
        try:
            final_url, _text, _status = net.http_get_text(
                url,
                timeout=5.0,
                max_redirects=5,
                headers={"User-Agent": net.UA_MOBILE},
                validator=_shortlink_hop,
            )
            if not is_platform_host(_host_of(final_url)):
                raise ShortlinkError("短链跳转到了不受支持的地址")
            return final_url
        except ShortlinkError:
            raise
        except net.NetError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    raise ShortlinkError("短链已失效，请重新复制完整链接") from last_error


# ---------------------------------------------------------------- 适配器注册


def get_adapter(platform: str) -> Any:
    """按平台取适配器实例（惰性导入，进程内缓存）。

    Args:
        platform: 平台标识。

    Returns:
        适配器实例（实现 ``parse`` / ``normalize`` / ``match``）。

    Raises:
        PlatformUnsupportedError: 未知平台。
    """
    name = (platform or "").strip().lower()
    if name not in PLATFORMS:
        raise PlatformUnsupportedError("暂不支持该平台，当前支持抖音、B站、快手")
    with _adapter_lock:
        adapter = _adapters.get(name)
        if adapter is not None:
            return adapter
        module = importlib.import_module(_ADAPTER_MODULES[name])
        adapter = getattr(module, "ADAPTER", None)
        if adapter is None:
            raise PlatformUnsupportedError(f"平台 {name} 的适配器未正确注册")
        _adapters[name] = adapter
        return adapter


def iter_platforms() -> list[dict[str, Any]]:
    """构造 ``GET /api/video/platforms`` 的平台列表（含开关与 Cookie 状态）。

    Returns:
        平台字典列表。
    """
    from app.modules.video import credentials

    video = get_settings().video
    cookies = credentials.cookie_status()
    items: list[dict[str, Any]] = []
    for name, hosts in PLATFORMS.items():
        cookie = cookies.get(name) or {}
        items.append(
            {
                "name": name,
                "label": PLATFORM_LABELS.get(name, name),
                "enabled": bool(video.platform_enabled(name)),
                "hosts": list(hosts),
                "capabilities": dict(PLATFORM_CAPABILITIES.get(name, {})),
                "cookie": {
                    "configured": bool(cookie.get("configured")),
                    "masked": cookie.get("masked"),
                },
            }
        )
    return items


def reset_for_tests() -> None:
    """清空适配器缓存与 DNS 缓存（测试用）。"""
    with _adapter_lock:
        _adapters.clear()
    with _dns_lock:
        _dns_cache.clear()


__all__ = [
    "PLATFORMS",
    "PLATFORM_CAPABILITIES",
    "PLATFORM_LABELS",
    "SHORTLINK_HOSTS",
    "expand_shortlink",
    "extract_links",
    "get_adapter",
    "is_platform_host",
    "iter_platforms",
    "platform_for_url",
    "reset_for_tests",
    "validate_hop",
    "validate_public_url",
]
