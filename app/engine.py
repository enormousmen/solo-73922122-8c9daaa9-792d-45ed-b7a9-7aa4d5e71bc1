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
"""

from __future__ import annotations

from itertools import product
from typing import Any

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


def evaluate(
    plan: PlanIn,
    confirmed: set[str],
    uncertain: set[str],
) -> dict[str, Any]:
    """穷举所有不确定设备的新旧组合并核算。

    :param plan: 录入的换路方案（拓扑、容量、入口流量、新旧路由）
    :param confirmed: 已登记成功回执的设备，强制新路由
    :param uncertain: 本批设备 ∪ 已放行未确认设备，新旧均可能
    :return: 含 scenarios / reachable / fault_summary / approved 的结果字典
    """
    capacity_map = _build_capacity_map(plan)
    old_route, new_route = _build_route_maps(plan)
    router_set = set(plan.routers)

    uncertain_sorted = sorted(uncertain)
    n = len(uncertain_sorted)
    if n > 12:  # 防御：录入校验已限制 12 台
        raise ValueError("不确定设备不能超过 12 台")

    scenarios: list[dict[str, Any]] = []
    fault_summary: dict[str, int] = {"断路": 0, "环路": 0, "超载": 0}
    all_safe = True

    for bits in product((False, True), repeat=n):
        # True 表示该不确定设备已切到新路由，False 表示仍是旧路由
        device_states = {
            device: ("new" if use_new else "old")
            for device, use_new in zip(uncertain_sorted, bits)
        }
        index = len(scenarios)

        def next_hop(node: str) -> str | None:
            """返回节点在本场景下的下一跳；出口返回 None。"""
            if node == plan.exit_node:
                return None
            if node not in router_set:
                return None  # 非路由器且非出口（理论上录入校验已拦截）
            if node in confirmed:
                return new_route[node]
            if node in uncertain:
                return new_route[node] if device_states[node] == "new" else old_route[node]
            return old_route[node]

        faults: list[dict[str, Any]] = []
        seen_faults: set[tuple[str, str]] = set()
        load: dict[tuple[str, str], float] = {}

        for ingress in plan.ingresses:
            # 沿当前场景的路由表逐跳追踪到出口
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

        reachable = not faults
        if not reachable:
            all_safe = False
            for fault in faults:
                fault_summary[fault["type"]] += 1

        scenarios.append(
            {
                "index": index,
                "device_states": device_states,
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
    }
