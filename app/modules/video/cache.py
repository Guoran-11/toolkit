"""解析结果内存缓存。

契约 §3.2 / §4.6 要求同一 URL 的解析结果缓存 10 分钟：用户反复点「解析」或同一
作品被批量提交时不应重复请求平台——每一次多余请求都在增加触发风控的概率。

实现要点：
    · ``OrderedDict`` + TTL，容量上限做 LRU 淘汰，避免长期运行后内存无界增长；
    · 只缓存 ``VideoItem`` 对象（含媒体直链），进程重启即失效，不落盘（直链有时效，
      落盘反而会让下次下载拿到过期地址）；
    · 值为轻量对象，读写都必须持锁（解析发生在请求线程与任务线程中）。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from app.core.config import get_settings


class ParseCache:
    """带 TTL 与容量上限的解析缓存（线程安全）。"""

    def __init__(self, max_items: int = 512) -> None:
        """初始化缓存。

        Args:
            max_items: 最多缓存的条目数，超出后按最久未使用淘汰。
        """
        self.max_items = max(1, int(max_items))
        self._lock = threading.RLock()
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        """读取缓存并刷新 LRU 顺序。

        Args:
            key: 缓存键（``platform:item_id`` 或归一化 URL）。

        Returns:
            未过期时返回缓存值；不存在或已过期返回 None。
        """
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expire_at, value = entry
            if expire_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, value: Any, ttl: int | None = None) -> None:
        """写入缓存。

        Args:
            key: 缓存键。
            value: 缓存值（通常为 VideoItem）。
            ttl: 有效期秒数；None 时读 ``settings.video.parse_cache_ttl_sec``。
        """
        if ttl is None:
            ttl = int(get_settings().video.parse_cache_ttl_sec)
        if ttl <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def clear(self) -> None:
        """清空缓存（测试或平台下线时使用）。"""
        with self._lock:
            self._items.clear()

    def size(self) -> int:
        """当前缓存条目数。

        Returns:
            条目数量（含未过期与尚未清理的过期条目）。
        """
        with self._lock:
            return len(self._items)


#: 进程级解析缓存单例
parse_cache = ParseCache()


__all__ = ["ParseCache", "parse_cache"]
