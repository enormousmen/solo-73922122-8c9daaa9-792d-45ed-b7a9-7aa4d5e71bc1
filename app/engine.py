"""换路安全核算引擎。

裁决模型（对每个目的网段独立核算）：
- confirmed（已登记成功回执的设备）：强制按 **新路由** 转发；
- pending（本批之外、已放行但尚未确认的设备）：新旧路由都可能生效；
- 本批申请设备：裁决期间同样尚未切换，新旧路由都可能生效；
- 其余设备：保持 **旧路由**。

因此 pending 设备 ∪ 本批设备构成“不确定设备”集合 U（|U| <= 12），
需要穷举它们的 2^|U| 种新旧组合。任一组合下出现断路、环路或链路超载，
则整批退回；全部组合安全才放行。这样无论这些设备以什么先后顺序切换，
流量都能到达出口且每条有向链路的合计流量不超过容量。

缓存语义（配合 :mod:`app.cache`）：
每个穷举场景对应方案内**每台**设备的一份完整新旧状态（confirmed 强制 new、
不确定设备按穷举位取 old/new、其余设备保持 old）。核算前先按这份完整状态查
进程内缓存：命中则直接复用历史核算结果，未命中才逐跳追踪并写回缓存。因此
缓存复用不改变任何场景的编号、顺序与故障内容，与“全量核算”完全一致。
"""

from __future__ import annotations

from itertools import product
from typing import Any

from .cache import StateKey
from .models import PlanIn

# 浮点比较容差
EPS = 1e-9
# 单批最多 12 台路由器 => 2^12 = 4096 个场景
MAX_SCENARIOS = 1 << 12
# 退回时回传的故障场景上限，避免响应体过大
MAX_REPORTED_SCENARIOS = 100


def _build_capacity_map(plan: PlanIn) -> dict[tuple[str, str], float]:
    return {(link.src, link.dst): link.capacity for link in plan.links}


def _build_route_maps(plan: PlanIn) -> tuple[dict[str, str], dict[str, str]]:
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    for route in plan.routes:
        old[route.device] = route.old_next_hop
        new[route.device] = route.new_next_hop
    return old, new


def _trace_state(
    plan: PlanIn,
    state_of: dict[str, str],
    capacity_map: dict[tuple[str, str], float],
    old_route: dict[str, str],
    new_route: dict[str, str],
    router_set: set[str],
) -> list[dict[str, Any]]:
    """对“每台设备走新路还是旧路”的一份完整状态做逐跳追踪与容量核算。

    :param state_of: 方案内每台路由器的完整新旧选择（"old" / "new"）
    :return: 该状态下的全部故障；空列表表示本状态安全
    """

    def next_hop(node: str) -> str | None:
        """返回节点在本状态下的下一跳；出口返回 None。"""
        if node == plan.exit_node:
            return None
        if node not in router_set:
            return None  # 非路由器且非出口（理论上录入校验已拦截）
        return new_route[node] if state_of[node] == "new" else old_route[node]

    faults: list[dict[str, Any]] = []
    seen_faults: set[tuple[str, str]] = set()
    load: dict[tuple[str, str], float] = {}

    for ingress in plan.ingresses:
        # 沿当前状态的路由表逐跳追踪到出口
        path: list[str] = [ingress.node]
        path_index: dict[str, int] = {ingress.node: 0}
        current = ingress.node
        open_location: str | None = None
        loop_location: str | None = None

        while current != plan.exit_node:
            hop = next_hop(current)
            if hop is None:
                open_location = current
                break
            edge = (current, hop)
            if edge not in capacity_map:
                # 下一跳可达但两者间没有录入有向链路 => 断路
                open_location = f"{current} -> {hop}"
                break
            if hop in path_index:
                # 再次进入路径上的节点 => 环路；location 展示从入口开始的完整转发路径
                loop_location = " -> ".join(path + [hop])
                break
            path_index[hop] = len(path)
            path.append(hop)
            load[edge] = load.get(edge, 0.0) + ingress.traffic
            current = hop

        if open_location is not None:
            key = ("断路", open_location)
            if key not in seen_faults:
                seen_faults.add(key)
                faults.append(
                    {
                        "type": "断路",
                        "location": open_location,
                        "detail": f"入口 {ingress.node} 的流量在 {open_location} 处无法继续转发到出口 {plan.exit_node}",
                    }
                )
        elif loop_location is not None:
            key = ("环路", loop_location)
            if key not in seen_faults:
                seen_faults.add(key)
                faults.append(
                    {
                        "type": "环路",
                        "location": loop_location,
                        "detail": f"入口 {ingress.node} 的流量沿 {loop_location} 形成转发环路，无法到达出口 {plan.exit_node}",
                    }
                )

    # 容量核算：每条有向链路上的合计入口流量不得超过容量
    for (src, dst), total in sorted(load.items()):
        capacity = capacity_map[(src, dst)]
        if total > capacity + EPS:
            location = f"{src} -> {dst}"
            key = ("超载", location)
            if key not in seen_faults:
                seen_faults.add(key)
                faults.append(
                    {
                        "type": "超载",
                        "location": location,
                        "detail": f"链路 {location} 合计流量 {total:g} 超过容量 {capacity:g}",
                        "load": round(total, 6),
                        "capacity": capacity,
                    }
                )

    return faults


def evaluate(
    plan: PlanIn,
    confirmed: set[str],
    uncertain: set[str],
    prefix: str | None = None,
    plan_version: int = 0,
    cache: Any | None = None,
) -> dict[str, Any]:
    """穷举所有不确定设备的新旧组合并核算。

    每个场景先构造方案内每台设备的完整新旧状态（confirmed 强制新路由、
    uncertain 按穷举位取新旧、其余设备保持旧路由），再以该完整状态为键查询
    进程内缓存：命中直接复用历史核算结果，未命中才逐跳核算并写回缓存。
    场景的编号与枚举顺序与不启用缓存时完全一致，因此放行结论、故障统计与
    故障示例与全量核算逐字一致。

    :param plan: 录入的换路方案（拓扑、容量、入口流量、新旧路由）
    :param confirmed: 已登记成功回执的设备，强制新路由
    :param uncertain: 本批设备 ∪ 已放行未确认设备，新旧均可能
    :param prefix: 目的网段（缓存键的一部分，API 调用必传）
    :param plan_version: 方案版本号，覆盖方案后递增以隔离旧缓存
    :param cache: 状态结果缓存实例（API 传入进程内单例）；默认 None 即不启用缓存，
                  每次都全量核算（便于引擎单测做对照）
    :return: 含 scenarios / reachable / fault_summary / approved 的结果字典，
             另含 states_computed（本次实际核算的状态数）与 cache_hits
    """
    capacity_map = _build_capacity_map(plan)
    old_route, new_route = _build_route_maps(plan)
    router_set = set(plan.routers)
    routers_sorted = sorted(plan.routers)

    uncertain_sorted = sorted(uncertain)
    n = len(uncertain_sorted)
    if n > 12:  # 防御：录入校验已限制 12 台
        raise ValueError("不确定设备不能超过 12 台")

    scenarios: list[dict[str, Any]] = []
    fault_summary: dict[str, int] = {"断路": 0, "环路": 0, "超载": 0}
    all_safe = True
    states_computed = 0
    cache_hits = 0

    for bits in product((False, True), repeat=n):
        # True 表示该不确定设备已切到新路由，False 表示仍是旧路由
        device_states = {
            device: ("new" if use_new else "old")
            for device, use_new in zip(uncertain_sorted, bits)
        }
        index = len(scenarios)

        # 方案内每台设备的完整新旧状态：confirmed 强制 new；不确定设备按穷举位；
        # 其余设备保持 old。每台设备的新旧选择都一致才是同一可缓存状态。
        full_states: list[str] = []
        for device in routers_sorted:
            if device in confirmed:
                full_states.append("new")
            elif device in device_states:
                full_states.append(device_states[device])
            else:
                full_states.append("old")
        states_tuple = tuple(full_states)
        cache_key = StateKey(prefix or "", plan_version, states_tuple)

        if cache is not None:
            cached = cache.get(cache_key)
        else:
            cached = None
        if cached is not None:
            cache_hits += 1
            faults = cached["faults"]
        else:
            state_of = dict(zip(routers_sorted, states_tuple))
            faults = _trace_state(
                plan, state_of, capacity_map, old_route, new_route, router_set
            )
            states_computed += 1
            if cache is not None:
                cache.put(cache_key, {"faults": faults})

        reachable = not faults
        if not reachable:
            all_safe = False
            for fault in faults:
                fault_summary[fault["type"]] += 1

        scenarios.append(
            {
                "index": index,
                # 仅回传不确定设备的新旧选择（confirmed/其余设备的路由是强制的）
                "device_states": dict(device_states),
                "reachable": reachable,
                "faults": faults,
            }
        )

    failing = [s for s in scenarios if not s["reachable"]]
    return {
        "approved": all_safe,
        "scenarios": scenarios,
        "scenarios_evaluated": len(scenarios),
        "failing_scenarios": failing,
        "failing_scenarios_count": len(failing),
        "fault_summary": fault_summary,
        "states_computed": states_computed,
        "cache_hits": cache_hits,
    }
