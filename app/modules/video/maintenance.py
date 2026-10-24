"""视频模块维护：清理过期历史与残留临时目录。

``app/main.py`` 的后台维护协程每 5 分钟调用一次 :func:`sweep`。契约 §4.4 要求
``storage/temp/video-<task_id>`` 在任务结束后由任务自己删除；但进程崩溃/强制重启时
``finally`` 不会执行，因此必须有一条按时间的兜底清理链（NFR-03：禁止磁盘泄漏）。

本函数**永不抛错**（逐项 try/except + 汇总），因为维护循环里任何未捕获异常都会让
后续所有清理永久停摆。
"""

from __future__ import annotations

import logging
import shutil
import time

from app.core.config import TEMP_DIR
from app.modules.video.history import history_store

logger = logging.getLogger(__name__)

#: 残留临时目录的保留时长（小时）。契约 §4.4：保留 24h 供断点续传/排障
_TEMP_MAX_AGE_HOURS = 24
#: 历史记录硬保留时长（小时）：产物 TTL 之外的兜底，失败记录也靠它回收
_HISTORY_MAX_AGE_HOURS = 168


def _sweep_temp_dirs(max_age_hours: int = _TEMP_MAX_AGE_HOURS) -> int:
    """清理超龄的 ``video-*`` 临时目录。

    Args:
        max_age_hours: 目录最后修改时间超过该小时数即删除。

    Returns:
        删除的目录数量。
    """
    if not TEMP_DIR.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        candidates = list(TEMP_DIR.glob("video-*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if not path.is_dir():
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def sweep() -> int:
    """执行一次视频模块维护。

    Returns:
        被清理的条目总数（历史记录 + 临时目录）。
    """
    total = 0
    try:
        total += history_store.expire(max_age_hours=_HISTORY_MAX_AGE_HOURS)
    except Exception as exc:  # noqa: BLE001 - 维护失败只记录，不影响主循环
        logger.warning("视频历史清理失败：%s", exc)

    try:
        total += _sweep_temp_dirs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("视频临时目录清理失败：%s", exc)

    return total


__all__ = ["sweep"]
