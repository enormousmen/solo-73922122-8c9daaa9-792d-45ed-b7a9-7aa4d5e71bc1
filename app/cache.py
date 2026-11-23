"""进程内 LRU 缓存：缓存单份“全设备新旧状态”的核算结果。

缓存键为 ``(prefix, plan_version, states)``：

- ``prefix`` 区分目的网段，缓存不跨网段复用；
- ``plan_version`` 在每次全量覆盖方案时递增，保证覆盖方案后旧结果永远不会命中，
  同时 ``invalidate_prefix`` 会主动清理该网段的陈旧条目以释放缓存位；
- ``states`` 是按路由器名排序后、方案内**每台**设备走新路（new）还是旧路（old）
  的完整状态元组——每台设备的新旧选择都一致，才算同一状态。

单进程内跨请求共享，最多保留 ``MAX_CACHED_STATES``（256）份状态结果；
超出容量时按最近最少使用（LRU）淘汰。所有访问都在 API 的同一把 asyncio 锁内、
以同步代码完成，因此无需额外加锁。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, NamedTuple

# 单进程跨请求最多缓存 256 份状态结果
MAX_CACHED_STATES = 256


class StateKey(NamedTuple):
    """一份状态结果的缓存键。"""

    prefix: str
    plan_version: int
    # 与 sorted(plan.routers) 对齐的新旧选择元组，如 ("old", "new", "old")
    states: tuple[str, ...]


class StateResultCache:
    """有限容量的 LRU 缓存；命中时刷新为最近使用，满容量时淘汰最久未用条目。"""

    def __init__(self, maxsize: int = MAX_CACHED_STATES) -> None:
        self._entries: OrderedDict[StateKey, dict[str, Any]] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: StateKey) -> dict[str, Any] | None:
        """命中返回状态结果并刷新 LRU 位置；未命中返回 None。"""
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: StateKey, entry: dict[str, Any]) -> None:
        """写入（或覆盖）一份状态结果，超出容量时淘汰最久未用条目。"""
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = entry
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def invalidate_prefix(self, prefix: str) -> int:
        """清理某网段的全部缓存条目（方案覆盖时调用），返回清理条数。"""
        stale = [key for key in self._entries if key.prefix == prefix]
        for key in stale:
            del self._entries[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()


# 进程内单例：跨请求复用，单 worker 部署（见 Dockerfile / docker-compose.yml）
state_cache = StateResultCache()
