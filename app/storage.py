"""进程内存储与并发控制。

每个目的网段维护独立的放行状态；所有写操作在同一把 asyncio 锁内完成，
保证并发申请按“裁决时刻已登记的批次和回执”串行裁决：后一个申请看到的
一定是前一个申请裁决并落库后的状态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import ApplicationOut, PlanIn


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PrefixState:
    plan: PlanIn
    # 已放行且已登记成功回执：按新路由转发
    confirmed: set[str] = field(default_factory=set)
    # 已放行但尚未确认：新旧路由都可能生效
    pending: set[str] = field(default_factory=set)
    # 回执编号留痕
    receipts: dict[str, str] = field(default_factory=dict)
    # 全部申请历史（含退回记录，退回不改变放行状态）
    applications: list[ApplicationOut] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


class Store:
    def __init__(self) -> None:
        self._prefixes: dict[str, PrefixState] = {}
        # 单把全局锁：临界区内只做内存计算，持锁时间短（最坏 4096 场景）
        self.lock = asyncio.Lock()

    async def get(self, prefix: str) -> PrefixState | None:
        return self._prefixes.get(prefix)

    async def require(self, prefix: str) -> PrefixState:
        state = self._prefixes.get(prefix)
        if state is None:
            raise KeyError(prefix)
        return state

    async def put_plan(self, plan: PlanIn) -> PrefixState:
        """全量覆盖录入；覆盖后原放行记录重置（重新核算的基线）。"""
        existing = self._prefixes.get(plan.prefix)
        if existing is None:
            state = PrefixState(plan=plan)
            self._prefixes[plan.prefix] = state
        else:
            existing.plan = plan
            existing.confirmed = set()
            existing.pending = set()
            existing.receipts = {}
            existing.applications = []
            existing.updated_at = utc_now_iso()
            state = existing
        return state

    async def list_prefixes(self) -> list[PrefixState]:
        return list(self._prefixes.values())


store = Store()
