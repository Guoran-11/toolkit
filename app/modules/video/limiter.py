"""限流与平台级熔断。

契约 §4.6：
    · 解析：20 次/分钟、100 次/小时；下载：10 次/分钟、50 次/小时（按 ``ip + user``）。
    · 同一平台连续 5 次 ``RISK_CONTROL`` / ``PARSE_FAILED`` → 冷却 10 分钟，期间提交与
      解析立即返回 ``RISK_CONTROL``（带 ``retry_after``）。

为什么用「内存时间戳队列」而不是第三方限流库：本模块不新增依赖，且单进程部署下
滑动窗口队列足够精确；队列长度被窗口上限硬性截断（每次命中先清理过期时间戳），
内存占用上限为 ``limit × 键数量``，不会无界增长。

**已知边界**：多进程/多 worker 部署时各进程独立计数，实际阈值会放大到 N 倍。
V1.0 是单进程离线单机版，该边界可接受；若未来改为多 worker，应换用共享存储。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Final

from app.core.errors import RateLimitedError, RiskControlError
from app.core.tasks import epoch_to_iso

# (窗口上限, 窗口秒数)：分钟 + 小时双重限制
_PARSE_WINDOWS: Final[tuple[tuple[int, int], ...]] = ((20, 60), (100, 3600))
_DOWNLOAD_WINDOWS: Final[tuple[tuple[int, int], ...]] = ((10, 60), (50, 3600))

# 连续失败阈值与冷却时长
FAILURE_THRESHOLD: Final[int] = 5
COOLDOWN_SEC: Final[int] = 600

_lock = threading.RLock()
# (bucket, window_seconds) -> identity -> 时间戳队列
_events: dict[tuple[str, int], dict[str, deque[float]]] = {}
# platform -> 连续失败次数
_failures: dict[str, int] = {}
# platform -> 冷却截止时间（epoch 秒）
_cooldown_until: dict[str, float] = {}


def _identity(ip: str | None, user: Any) -> str:
    """构造限流维度键。

    Args:
        ip: 客户端 IP。
        user: 鉴权主体（字典或字符串）。

    Returns:
        形如 ``127.0.0.1|admin`` 的字符串。
    """
    if isinstance(user, dict):
        name = str(user.get("sub") or user.get("username") or "anonymous")
    else:
        name = str(user or "anonymous")
    return f"{ip or 'unknown'}|{name}"


def _hit(bucket: str, ip: str | None, user: Any, windows: tuple[tuple[int, int], ...]) -> None:
    """对某个维度做滑动窗口计数，超限抛错。

    Args:
        bucket: 桶名（``parse`` / ``download``）。
        ip: 客户端 IP。
        user: 鉴权主体。
        windows: ``(上限, 窗口秒数)`` 序列。

    Raises:
        RateLimitedError: 任一窗口超限，``extra`` 带 ``retry_after``。
    """
    identity = _identity(ip, user)
    now = time.time()
    with _lock:
        queues: list[deque[float]] = []
        for limit, window in windows:
            store = _events.setdefault((bucket, window), {})
            queue = store.setdefault(identity, deque())
            while queue and queue[0] <= now - window:
                queue.popleft()
            if len(queue) >= limit:
                retry_after = max(1, int(queue[0] + window - now) + 1)
                raise RateLimitedError(
                    f"操作太频繁，请 {retry_after} 秒后再试",
                    extra={"retry_after": retry_after},
                )
            queues.append(queue)
        for queue in queues:
            queue.append(now)


def check_parse(ip: str | None, user: Any) -> None:
    """解析限流检查（20/分、100/时）。

    Args:
        ip: 客户端 IP。
        user: 鉴权主体。

    Raises:
        RateLimitedError: 超限。
    """
    _hit("parse", ip, user, _PARSE_WINDOWS)


def check_download(ip: str | None, user: Any) -> None:
    """下载限流检查（10/分、50/时）。

    Args:
        ip: 客户端 IP。
        user: 鉴权主体。

    Raises:
        RateLimitedError: 超限。
    """
    _hit("download", ip, user, _DOWNLOAD_WINDOWS)


def record_platform_failure(platform: str) -> None:
    """记录一次平台级失败；达到阈值后开启冷却。

    Args:
        platform: 平台标识。
    """
    if not platform:
        return
    with _lock:
        count = _failures.get(platform, 0) + 1
        _failures[platform] = count
        if count >= FAILURE_THRESHOLD:
            _cooldown_until[platform] = time.time() + COOLDOWN_SEC
            # 计数清零，冷却结束后重新累计，避免一次恢复失败立刻再次熔断
            _failures[platform] = 0


def record_platform_success(platform: str) -> None:
    """记录一次平台级成功，清空失败计数与冷却状态。

    Args:
        platform: 平台标识。
    """
    if not platform:
        return
    with _lock:
        _failures.pop(platform, None)
        _cooldown_until.pop(platform, None)


def assert_platform_available(platform: str) -> None:
    """断言平台不在熔断冷却期。

    Args:
        platform: 平台标识。

    Raises:
        RiskControlError: 平台处于冷却期；``extra`` 带 ``retry_after``。
    """
    now = time.time()
    with _lock:
        until = _cooldown_until.get(platform)
        if until is None:
            return
        if until <= now:
            _cooldown_until.pop(platform, None)
            return
        retry_after = max(1, int(until - now) + 1)
    raise RiskControlError(
        f"平台风控中，请 {retry_after} 秒后再试",
        extra={"retry_after": retry_after},
    )


def snapshot() -> dict[str, Any]:
    """导出限流/熔断当前状态（供设置页或排障使用）。

    Returns:
        含 ``limits`` 与 ``platforms`` 的字典；冷却时间用 ISO 字符串表示。
    """
    now = time.time()
    with _lock:
        platforms: dict[str, Any] = {}
        names = set(_failures) | set(_cooldown_until)
        for name in sorted(names):
            until = _cooldown_until.get(name)
            remaining = max(0, int(until - now) + 1) if until and until > now else 0
            platforms[name] = {
                "failures": _failures.get(name, 0),
                "cooldown_until": epoch_to_iso(until) if remaining else None,
                "retry_after": remaining,
                "available": remaining == 0,
            }
        return {
            "limits": {
                "parse": [{"limit": limit, "window_sec": window} for limit, window in _PARSE_WINDOWS],
                "download": [
                    {"limit": limit, "window_sec": window} for limit, window in _DOWNLOAD_WINDOWS
                ],
            },
            "failure_threshold": FAILURE_THRESHOLD,
            "cooldown_sec": COOLDOWN_SEC,
            "platforms": platforms,
        }


def reset() -> None:
    """清空全部限流与熔断状态（测试用）。"""
    with _lock:
        _events.clear()
        _failures.clear()
        _cooldown_until.clear()


__all__ = [
    "COOLDOWN_SEC",
    "FAILURE_THRESHOLD",
    "assert_platform_available",
    "check_download",
    "check_parse",
    "record_platform_failure",
    "record_platform_success",
    "reset",
    "snapshot",
]
