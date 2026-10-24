"""系统级端点：健康检查与引擎自检。

``/healthz`` 是前端的**启动探测入口**（``config.js`` 的 ``probe.path``，
超时 1.5 秒）。它决定界面进入「直连模式」还是「演示模式」，因此有一条硬性要求：

    **绝不做任何可能超时的阻塞工作。**

具体做法：只读引擎探测的缓存，缓存未就绪时返回 ``unknown`` 状态，
由启动预热或自检页的强制刷新来填充。若在这里直接跑一遍探测，
LibreOffice 冷启动的数百毫秒加上并发上传的干扰，很容易突破 1.5 秒。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import ConfigDep, GuardDep, OptionalUserDep
from app.core.config import BASE_DIR
from app.core.engines import engine_probe, runtime_info
from app.core.security import credential_vault
from app.core.storage import disk_free_bytes, registry
from app.core.tasks import get_task_manager
from app.modules.ssh.session import session_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

# 进程启动时刻（用于计算 uptime）
_STARTED_AT: float = time.time()

# 磁盘水位告警阈值：1 GiB（NFR-06）
DISK_WARN_BYTES: int = 1024**3


def _engine_payload(*, allow_probe: bool = False) -> dict[str, dict[str, Any]]:
    """构造引擎状态字典。

    Args:
        allow_probe: 是否允许在无缓存时主动探测。仅自检页使用。

    Returns:
        引擎名 → 状态字典。
    """
    cached = engine_probe.cached()
    if cached:
        return {name: status.to_api() for name, status in cached.items()}

    if allow_probe:
        return engine_probe.api_payload()

    # 无缓存且不允许探测：返回 unknown，不阻塞响应
    return {
        name: {"state": "unknown", "version": None, "path": None, "hint": "引擎状态尚未探测，请稍后重试。"}
        for name in ("libreoffice", "chromium", "ffmpeg", "ytdlp")
    }


@router.get("/healthz")
def healthz(request: Request, settings: ConfigDep, user: OptionalUserDep) -> dict[str, Any]:
    """健康检查（★ 前端启动探测入口，不需要鉴权）。

    **鉴权开启且未携带有效 Token 时只返回最小事实**：服务在线、需要登录。
    磁盘余量、引擎清单、任务计数、运行时长等运行细节一律不下发——未登录访客
    没有任何理由看到这套部署的家底（版本号与服务是否在线是登录页本身就要用的）。

    字段名刻意保持不变，敏感值退化为 ``None`` / ``unknown``，因此前端
    ``probeBackend`` 的读取逻辑无需分支：未登录时状态栏显示「磁盘 —」与灰色的
    引擎指示灯，登录后由前端补拉一次带 Token 的同款请求即可拿到完整数据。

    Args:
        request: 请求对象（用于解析可选登录态）。
        settings: 全局配置。
        user: 已解析的登录态；未登录为 None。

    Returns:
        含以下字段的字典（字段名与前端 ``api.js`` 的读取逻辑严格对齐）：
            ``ok`` / ``version`` / ``disk_free_bytes`` / ``engines`` / ``auth_required``
    """
    if settings.server.auth_required and user is None:
        return {
            "ok": True,
            "version": settings.app.version,
            "codename": settings.app.codename,
            "auth_required": True,
            "authenticated": False,
            "demo": False,
            # None 而不是 0：前端据此显示「磁盘 —」，不会误报磁盘告警
            "disk_free_bytes": None,
            "disk_warning": False,
            "engines": {
                name: {
                    "state": "unknown",
                    "version": None,
                    "path": None,
                    "hint": "登录后可查看引擎状态。",
                }
                for name in ("libreoffice", "chromium", "ffmpeg")
            },
            "uptime_sec": None,
            "tasks": None,
        }

    free = disk_free_bytes(BASE_DIR)
    stats = get_task_manager().stats()

    return {
        "ok": True,
        "version": settings.app.version,
        "codename": settings.app.codename,
        "auth_required": settings.server.auth_required,
        "authenticated": True,
        "demo": False,
        "disk_free_bytes": free,
        "disk_warning": free < DISK_WARN_BYTES,
        "engines": _engine_payload(),
        "uptime_sec": int(time.time() - _STARTED_AT),
        "tasks": stats,
    }


@router.get("/api/system/engines")
def system_engines(_user: GuardDep, settings: ConfigDep) -> dict[str, Any]:
    """引擎自检（FR-SYS-06）。

    与 ``/healthz`` 的区别：本端点**允许阻塞探测**。它是用户在设置页主动点开的
    「引擎自检」，多等半秒换取准确结果完全合理。

    Args:
        _user: 鉴权主体。
        settings: 全局配置。

    Returns:
        含 ``engines`` / ``runtime`` / ``paths`` / ``versions`` 的字典。
    """
    engines = _engine_payload(allow_probe=True)

    missing = [name for name, status in engines.items() if status.get("state") == "fail"]

    return {
        "engines": engines,
        "runtime": runtime_info(),
        "paths": {
            "base_dir": str(BASE_DIR),
            "storage": str(BASE_DIR / "storage"),
            "runtime": str(BASE_DIR / "runtime"),
        },
        "config": {
            "max_upload_mb": settings.limits.max_upload_mb,
            "max_audio_upload_mb": settings.limits.max_audio_upload_mb,
            "max_batch_files": settings.limits.max_batch_files,
            "workers": settings.limits.workers,
            "task_timeout_sec": settings.limits.task_timeout_sec,
            "output_ttl_hours": settings.limits.output_ttl_hours,
            "ssh_enabled": settings.ssh.enabled,
            "auth_required": settings.server.auth_required,
        },
        "missing_engines": missing,
        "summary": (
            "全部引擎就绪。" if not missing else f"以下引擎不可用，相关功能将不可用：{', '.join(missing)}"
        ),
    }


@router.post("/api/system/engines/refresh")
def refresh_engines(_user: GuardDep) -> dict[str, Any]:
    """强制重新探测引擎（自检页的「刷新」按钮）。

    Args:
        _user: 鉴权主体。

    Returns:
        重新探测后的引擎状态。
    """
    engine_probe.invalidate()
    return {"engines": _engine_payload(allow_probe=True)}


@router.get("/api/system/storage")
def system_storage(_user: GuardDep, settings: ConfigDep) -> dict[str, Any]:
    """存储与资源概览（供首页磁盘水位提示与运维巡检使用）。

    Args:
        _user: 鉴权主体。
        settings: 全局配置。

    Returns:
        含磁盘余量、产物数量、活跃会话与凭据数的字典。
    """
    free = disk_free_bytes(BASE_DIR)
    outputs = registry.list_all()
    total_bytes = sum(item.size for item in outputs)

    return {
        "disk_free_bytes": free,
        "disk_warning": free < DISK_WARN_BYTES,
        "disk_warn_threshold": DISK_WARN_BYTES,
        "outputs": {
            "count": len(outputs),
            "total_bytes": total_bytes,
            "ttl_hours": settings.limits.output_ttl_hours,
        },
        "tasks": get_task_manager().stats(),
        "ssh_sessions": session_registry.count(),
        "credentials_held": credential_vault.count(),
        "uptime_sec": int(time.time() - _STARTED_AT),
    }


def warm_up_engines() -> None:
    """在后台线程中预热引擎探测。

    由 ``main.py`` 的 lifespan 调用。放在后台线程的原因：
    探测要起子进程（LibreOffice 冷启动可能数百毫秒），会拖慢服务启动，
    而用户点开界面前并不需要这个结果。

    异常被刻意吞掉：引擎缺失是**预期内**的状态（用户可能只用 PDF 功能），
    不能让它成为服务启动失败的原因。
    """

    def _work() -> None:
        """执行探测。"""
        try:
            engine_probe.probe(force=True)
            logger.info("引擎探测预热完成")
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响服务可用性
            logger.warning("引擎预热失败：%s", exc)

    import threading

    threading.Thread(target=_work, name="engine-warmup", daemon=True).start()


__all__ = ["DISK_WARN_BYTES", "router", "warm_up_engines"]
