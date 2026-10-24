"""视频下载历史记录与产物复用（去重）。

契约 §3.4 / §5.3：
    · 落盘 ``storage/video_history.json``，结构 ``{"items": [...]}``；
    · 原子写（``.tmp`` + ``os.replace``），上限 ``settings.video.history_limit``（FIFO 淘汰）；
    · 24 小时窗口内同 ``platform + item_id`` 且 ``status=success``、产物未过期 → 可复用；
    · **绝不保存媒体直链**（会过期且可能带签名），只保存作品长链与 ``file_id``。

线程安全：解析、下载任务线程、维护协程都会读写历史，统一用 ``RLock`` 串行化。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.core.config import STORAGE_DIR, get_settings
from app.core.tasks import utc_now_iso

logger = logging.getLogger(__name__)

_HISTORY_FILE: Final[Path] = STORAGE_DIR / "video_history.json"

#: API 输出字段（顺序即前端表格列顺序）；``url`` 只落盘不对外输出
_API_FIELDS: Final[tuple[str, ...]] = (
    "platform",
    "item_id",
    "title",
    "author",
    "cover_url",
    "quality",
    "size_bytes",
    "status",
    "task_id",
    "file_id",
    "created_at",
    "expire_at",
)


def _parse_iso(value: str | None) -> float | None:
    """把 ISO 8601 字符串解析为 epoch 秒。

    Args:
        value: 形如 ``2026-10-20T13:10:13.123Z`` 的字符串。

    Returns:
        epoch 秒；解析失败或为空返回 None。
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class VideoHistory:
    """历史记录存储（线程安全 + JSON 原子持久化）。"""

    def __init__(self, path: Path | None = None, limit: int | None = None) -> None:
        """初始化。

        Args:
            path: 历史文件路径，默认 ``storage/video_history.json``。
            limit: 条数上限；None 时运行时读取配置（配置可热更新）。
        """
        self._path = path or _HISTORY_FILE
        self._limit_override = limit
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self._load()

    # -------------------------------------------------- 持久化

    def _limit(self) -> int:
        """当前条数上限。

        Returns:
            正整数上限。
        """
        if self._limit_override:
            return max(1, int(self._limit_override))
        return max(1, int(get_settings().video.history_limit))

    def _load(self) -> None:
        """从磁盘载入历史；文件损坏时以空历史启动（不阻塞服务）。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("视频历史文件损坏，已从空历史启动：%s", exc)
            return
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and item.get("platform") and item.get("item_id"):
                self._items.append(dict(item))

    def _flush(self) -> None:
        """把历史写回磁盘（原子替换）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            payload = {"items": self._items}
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("视频历史写入失败：%s", exc)

    def _trim(self) -> None:
        """按条数上限 FIFO 淘汰（调用方需持锁）。"""
        limit = self._limit()
        if len(self._items) > limit:
            del self._items[: len(self._items) - limit]

    # -------------------------------------------------- 写入

    def record(
        self,
        *,
        platform: str,
        item_id: str,
        title: str = "",
        author: str = "",
        cover_url: str | None = None,
        quality: str = "",
        size_bytes: int = 0,
        status: str = "success",
        task_id: str | None = None,
        file_id: str | None = None,
        url: str | None = None,
        expire_at: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """写入（或更新）一条历史记录。

        以 ``platform + item_id`` 为键做 upsert：同一作品重复下载时历史里仍只有
        一条（最新的那条），避免列表被同一作品刷屏。

        Args:
            platform: 平台标识。
            item_id: 作品 ID。
            title: 标题。
            author: 作者名。
            cover_url: 封面地址。
            quality: 下载清晰度。
            size_bytes: 产物字节数。
            status: 记录状态（success/failed）。
            task_id: 关联任务 ID。
            file_id: 产物 ID。
            url: 作品长链（只落盘，不对外输出）。
            expire_at: 产物过期时间（ISO 8601）。
            owner_id: 预留的多用户隔离字段。

        Returns:
            写入后的记录副本（内部结构，含 ``url``）。
        """
        now = utc_now_iso()
        entry: dict[str, Any] = {
            "platform": platform,
            "item_id": item_id,
            "title": title or "未命名作品",
            "author": author or "未知作者",
            "cover_url": cover_url,
            "quality": quality or None,
            "size_bytes": int(size_bytes or 0),
            "status": status,
            "task_id": task_id,
            "file_id": file_id,
            "url": url,
            "owner_id": owner_id,
            "created_at": now,
            "expire_at": expire_at,
        }
        with self._lock:
            self._items = [
                item
                for item in self._items
                if not (item.get("platform") == platform and item.get("item_id") == item_id)
            ]
            self._items.append(entry)
            self._trim()
            self._flush()
        return dict(entry)

    # -------------------------------------------------- 查询与复用

    def find_reusable(self, platform: str, item_id: str, window_hours: int = 24) -> dict[str, Any] | None:
        """查找窗口期内可复用的成功记录。

        Args:
            platform: 平台标识。
            item_id: 作品 ID。
            window_hours: 复用窗口（小时）；0 表示不允许复用。

        Returns:
            命中的记录副本；未命中返回 None。调用方还需用 ``registry.get(file_id)``
            确认产物文件仍存在且未过期。
        """
        if window_hours <= 0 or not platform or not item_id:
            return None
        now = time.time()
        cutoff = now - window_hours * 3600
        with self._lock:
            for item in reversed(self._items):
                if item.get("platform") != platform or item.get("item_id") != item_id:
                    continue
                if item.get("status") != "success" or not item.get("file_id"):
                    continue
                created = _parse_iso(item.get("created_at"))
                if created is None or created < cutoff:
                    continue
                expire = _parse_iso(item.get("expire_at"))
                if expire is not None and expire <= now:
                    continue
                return dict(item)
        return None

    def list_items(
        self,
        *,
        platform: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页查询历史（按创建时间倒序）。

        Args:
            platform: 平台过滤；空或 ``all`` 表示不过滤。
            keyword: 标题/作者关键词（大小写不敏感）。
            page: 页码，从 1 开始。
            page_size: 每页条数（1–100）。

        Returns:
            ``{"total", "page", "page_size", "items"}``；条目只含 API 字段。
        """
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        needle = (keyword or "").strip().lower()

        with self._lock:
            items = list(reversed(self._items))

        if platform and platform != "all":
            items = [item for item in items if item.get("platform") == platform]
        if needle:
            items = [
                item
                for item in items
                if needle in str(item.get("title", "")).lower()
                or needle in str(item.get("author", "")).lower()
            ]

        total = len(items)
        start = (page - 1) * page_size
        window = items[start : start + page_size]
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [{key: item.get(key) for key in _API_FIELDS} for item in window],
        }

    # -------------------------------------------------- 删除与维护

    def remove(self, item_id: str, platform: str | None = None) -> int:
        """删除单条历史（不删产物）。

        Args:
            item_id: 作品 ID。
            platform: 平台过滤；None 表示删除所有平台的同 ID 记录。

        Returns:
            删除条数。
        """
        with self._lock:
            before = len(self._items)
            self._items = [
                item
                for item in self._items
                if not (
                    item.get("item_id") == item_id
                    and (platform is None or item.get("platform") == platform)
                )
            ]
            removed = before - len(self._items)
            if removed:
                self._flush()
        return removed

    def clear(self) -> int:
        """清空全部历史（不删产物）。

        Returns:
            清理条数。
        """
        with self._lock:
            removed = len(self._items)
            self._items = []
            if removed:
                self._flush()
        return removed

    def expire(self, max_age_hours: int = 168) -> int:
        """清理过期历史。

        判定为过期的两种情况：产物已过 TTL，或记录创建时间超过 ``max_age_hours``。
        后者是兜底——失败记录没有 ``expire_at``，不靠它会被永久保留。

        Args:
            max_age_hours: 记录最大保留时长（小时）。

        Returns:
            清理条数。
        """
        now = time.time()
        cutoff = now - max(1, int(max_age_hours)) * 3600
        with self._lock:
            kept: list[dict[str, Any]] = []
            for item in self._items:
                created = _parse_iso(item.get("created_at"))
                expire = _parse_iso(item.get("expire_at"))
                if expire is not None and expire <= now:
                    continue
                if created is not None and created < cutoff:
                    continue
                kept.append(item)
            removed = len(self._items) - len(kept)
            if removed:
                self._items = kept
                self._flush()
        return removed

    def count(self) -> int:
        """当前记录数。

        Returns:
            记录条数。
        """
        with self._lock:
            return len(self._items)


#: 进程级历史单例
history_store = VideoHistory()


__all__ = ["VideoHistory", "history_store"]
