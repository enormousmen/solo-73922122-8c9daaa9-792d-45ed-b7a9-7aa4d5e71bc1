"""Pydantic 数据模型与录入校验。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_ROUTERS = 12


class LinkIn(BaseModel):
    """有向链路及其容量。"""

    src: str = Field(..., min_length=1, description="链路起点")
    dst: str = Field(..., min_length=1, description="链路终点")
    capacity: float = Field(..., gt=0, description="链路容量（>0）")


class IngressIn(BaseModel):
    """入口流量：从某个节点进入、去往出口的流量。"""

    node: str = Field(..., min_length=1, description="入口节点")
    traffic: float = Field(..., ge=0, description="入口流量（>=0）")


class RouteIn(BaseModel):
    """单台路由器的新旧下一跳。"""

    device: str = Field(..., min_length=1, description="路由器设备名")
    old_next_hop: str = Field(..., min_length=1, description="旧路由下一跳")
    new_next_hop: str = Field(..., min_length=1, description="新路由下一跳")


class PlanIn(BaseModel):
    """值班员为一个目的网段录入的换路方案（PUT 全量覆盖）。"""

    prefix: str = Field(..., min_length=1, description="目的网段，如 10.10.0.0/16")
    exit_node: str = Field(..., min_length=1, description="出口节点名")
    routers: list[str] = Field(..., min_length=1, max_length=MAX_ROUTERS, description="路由器列表，最多 12 台")
    links: list[LinkIn] = Field(..., min_length=1, description="有向链路及容量")
    ingresses: list[IngressIn] = Field(..., min_length=1, description="各入口流量")
    routes: list[RouteIn] = Field(..., min_length=1, description="每台路由器的新旧下一跳")

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "PlanIn":
        # 1. 路由器名唯一，出口不能与路由器重名
        if len(set(self.routers)) != len(self.routers):
            raise ValueError("routers 中存在重名设备")
        if self.exit_node in self.routers:
            raise ValueError("exit_node 不能与路由器重名")

        nodes = set(self.routers) | {self.exit_node}

        # 2. 链路端点合法、链路唯一
        seen_links: set[tuple[str, str]] = set()
        for link in self.links:
            if link.src not in nodes or link.dst not in nodes:
                raise ValueError(f"链路 {link.src} -> {link.dst} 的端点不在节点集合内")
            if (link.src, link.dst) in seen_links:
                raise ValueError(f"链路 {link.src} -> {link.dst} 重复录入")
            seen_links.add((link.src, link.dst))

        # 3. 入口合法、唯一，且入口不能就是出口
        seen_ingress: set[str] = set()
        for ing in self.ingresses:
            if ing.node not in nodes:
                raise ValueError(f"入口节点 {ing.node} 不在节点集合内")
            if ing.node == self.exit_node:
                raise ValueError("入口节点不能是出口节点")
            if ing.node in seen_ingress:
                raise ValueError(f"入口节点 {ing.node} 重复录入")
            seen_ingress.add(ing.node)

        # 4. 每台路由器恰好一条路由记录，下一跳合法
        if len(self.routes) != len(self.routers):
            raise ValueError("routes 数量必须与 routers 一致，每台路由器一条记录")
        route_devices: set[str] = set()
        for route in self.routes:
            if route.device not in self.routers:
                raise ValueError(f"路由记录中的设备 {route.device} 不在 routers 列表内")
            if route.device in route_devices:
                raise ValueError(f"设备 {route.device} 存在多条路由记录")
            route_devices.add(route.device)
            for hop in (route.old_next_hop, route.new_next_hop):
                if hop not in nodes:
                    raise ValueError(f"设备 {route.device} 的下一跳 {hop} 不在节点集合内")
        return self


class ApplicationIn(BaseModel):
    """换路申请：本批要求下发新配置的设备。"""

    devices: list[str] = Field(..., min_length=1, description="本批申请下发新配置的设备")

    @model_validator(mode="after")
    def _no_duplicates(self) -> "ApplicationIn":
        if len(set(self.devices)) != len(self.devices):
            raise ValueError("devices 中存在重复设备")
        return self


class ReceiptIn(BaseModel):
    """已放行设备切换成功的回执登记。"""

    device: str = Field(..., min_length=1, description="回执对应的设备")
    receipt_id: str | None = Field(None, description="外部回执编号，缺省由服务端生成")
    note: str | None = Field(None, description="备注")


class Fault(BaseModel):
    """单条故障：类型 + 故障位置 + 细节。"""

    type: Literal["断路", "环路", "超载"]
    location: str = Field(..., description="故障位置，如 r1 -> r3 或 r1 -> r2 -> r1")
    detail: str
    load: float | None = None
    capacity: float | None = None


class ScenarioOut(BaseModel):
    """一种不确定设备的新旧状态组合及其核算结果。"""

    index: int
    device_states: dict[str, Literal["old", "new"]]
    reachable: bool
    faults: list[Fault]


class ApplicationOut(BaseModel):
    """一次换路申请的裁决结果。"""

    application_id: str
    prefix: str
    decision: Literal["approved", "rejected"]
    batch: list[str]
    uncertain_devices: list[str]
    scenarios_evaluated: int
    failing_scenarios_count: int
    fault_summary: dict[str, int]
    failing_scenarios: list[ScenarioOut]
    failing_scenarios_truncated: bool
    message: str
    decided_at: str


class ReceiptOut(BaseModel):
    """回执登记结果。"""

    prefix: str
    device: str
    receipt_id: str
    status: Literal["registered", "already_registered"]
    confirmed_devices: list[str]
    pending_devices: list[str]
    recorded_at: str


class PlanSummary(BaseModel):
    prefix: str
    exit_node: str
    routers: list[str]
    confirmed_devices: list[str]
    pending_devices: list[str]
    application_count: int
    updated_at: str


class PlanDetail(PlanSummary):
    plan: PlanIn
    applications: list[ApplicationOut]
    created_at: str
