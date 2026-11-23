"""引擎与 API 的 pytest 自测。"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.engine import evaluate
from app.main import app
from app.models import PlanIn
from app.storage import store

client = TestClient(app)


def p(path_prefix: str, suffix: str) -> str:
    """网段前缀含 '/'，通过 query 参数传递，避免路径分段问题。"""
    return f"/plans/{suffix}?prefix={quote(path_prefix, safe='')}"


def q(path_prefix: str) -> str:
    return f"/plans/detail?prefix={quote(path_prefix, safe='')}"


def make_plan(
    prefix: str = "10.10.0.0/16",
    routers: list[str] | None = None,
    routes: list[dict] | None = None,
    links: list[dict] | None = None,
    ingresses: list[dict] | None = None,
    exit_node: str = "exit",
) -> PlanIn:
    routers = routers or ["r1", "r2", "r3", "r4"]
    # 演示拓扑（新旧路由用到的有向链路均已录入）：
    # r1: old r2 / new r3；r2: old r4 / new r1；r3: old r4 / new exit；r4: old exit / new r3
    routes = routes or [
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r4", "new_next_hop": "r1"},
        {"device": "r3", "old_next_hop": "r4", "new_next_hop": "exit"},
        {"device": "r4", "old_next_hop": "exit", "new_next_hop": "r3"},
    ]
    links = links or [
        {"src": "r1", "dst": "r2", "capacity": 100},
        {"src": "r1", "dst": "r3", "capacity": 100},
        {"src": "r2", "dst": "r4", "capacity": 100},
        {"src": "r2", "dst": "r1", "capacity": 100},
        {"src": "r3", "dst": "r4", "capacity": 100},
        {"src": "r3", "dst": "exit", "capacity": 100},
        {"src": "r4", "dst": "exit", "capacity": 100},
        {"src": "r4", "dst": "r3", "capacity": 100},
    ]
    ingresses = ingresses or [{"node": "r1", "traffic": 10}]
    return PlanIn(
        prefix=prefix,
        exit_node=exit_node,
        routers=routers,
        links=links,
        ingresses=ingresses,
        routes=routes,
    )


# ---------- 录入校验 ----------

def test_model_rejects_bad_next_hop() -> None:
    with pytest.raises(ValueError):
        make_plan(routes=[
            {"device": "r1", "old_next_hop": "ghost", "new_next_hop": "r3"},
            {"device": "r2", "old_next_hop": "r4", "new_next_hop": "r1"},
            {"device": "r3", "old_next_hop": "r4", "new_next_hop": "r4"},
            {"device": "r4", "old_next_hop": "exit", "new_next_hop": "exit"},
        ])


def test_model_rejects_missing_link_endpoint() -> None:
    with pytest.raises(ValueError):
        make_plan(links=[{"src": "r1", "dst": "ghost", "capacity": 10}])


def test_model_rejects_more_than_12_routers() -> None:
    routers = [f"r{i}" for i in range(13)]
    with pytest.raises(ValueError):
        PlanIn(
            prefix="p",
            exit_node="exit",
            routers=routers,
            links=[{"src": routers[0], "dst": "exit", "capacity": 1}],
            ingresses=[{"node": routers[0], "traffic": 1}],
            routes=[{"device": r, "old_next_hop": "exit", "new_next_hop": "exit"} for r in routers],
        )


def test_model_rejects_duplicate_routers() -> None:
    with pytest.raises(ValueError):
        make_plan(routers=["r1", "r1", "r3", "r4"])


# ---------- 引擎核算 ----------

def test_engine_safe_baseline() -> None:
    plan = make_plan()
    result = evaluate(plan, confirmed=set(), uncertain=set())
    assert result["approved"] is True
    assert result["scenarios_evaluated"] == 1


def test_engine_detects_loop() -> None:
    # r2 新路由指向 r1；r1 新路由指向 r3。若本批只放行 r2，
    # r2=new + r1=old => r1->r2->r1 环路
    plan = make_plan()
    result = evaluate(plan, confirmed=set(), uncertain={"r2"})
    assert result["approved"] is False
    assert result["fault_summary"]["环路"] >= 1
    loop = result["failing_scenarios"][0]["faults"][0]
    assert loop["type"] == "环路"
    assert "r1 -> r2 -> r1" in loop["location"]


def test_engine_receipt_removes_loop() -> None:
    plan = make_plan()
    # r3、r4 同在途：r3=old(->r4) 且 r4=new(->r3) 时 r3<->r4 环路
    result = evaluate(plan, confirmed=set(), uncertain={"r3", "r4"})
    assert result["approved"] is False
    assert result["fault_summary"]["环路"] >= 1

    # r3 的成功回执到达：r3 强制新路由 exit，不确定集合只剩 r4 =>
    # r4=old 走 exit，r4=new 走 r3->exit，两种状态都安全
    result_after = evaluate(plan, confirmed={"r3"}, uncertain={"r4"})
    assert result_after["approved"] is True
    assert result_after["failing_scenarios_count"] == 0

    # 另一处环路：r2 在途（new=r1），r1 保持旧路由 r2 => r1<->r2 环路
    result_loop2 = evaluate(plan, confirmed=set(), uncertain={"r2"})
    assert result_loop2["approved"] is False
    assert result_loop2["fault_summary"]["环路"] >= 1


def test_engine_detects_open_circuit() -> None:
    # r3 新下一跳 r2，但没有录入 r3->r2 有向链路 => 断路（单独构造，避免被环路掩盖）
    plan = make_plan(routes=[
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r4", "new_next_hop": "r4"},
        {"device": "r3", "old_next_hop": "r4", "new_next_hop": "r2"},
        {"device": "r4", "old_next_hop": "exit", "new_next_hop": "exit"},
    ])
    # r1、r3 同批申请：r1=new 把流量引到 r3，r3=new 时 r3->r2 无链路 => 断路
    result = evaluate(plan, confirmed=set(), uncertain={"r1", "r3"})
    assert result["approved"] is False
    assert result["fault_summary"]["断路"] >= 1
    open_faults = [
        f for s in result["failing_scenarios"] for f in s["faults"] if f["type"] == "断路"
    ]
    assert any(f["location"] == "r3 -> r2" for f in open_faults)


def test_engine_detects_overload() -> None:
    # r1 与 r3 各注入 60，r3 保持旧路由时全部汇聚到 r4->exit（容量 100）=> 120 > 100
    plan = make_plan(ingresses=[{"node": "r1", "traffic": 60}, {"node": "r3", "traffic": 60}])
    links = {(l.src, l.dst): l.capacity for l in plan.links}
    assert links[("r4", "exit")] == 100
    result = evaluate(plan, confirmed=set(), uncertain=set())
    assert result["approved"] is False
    fault = result["failing_scenarios"][0]["faults"][0]
    assert fault["type"] == "超载"
    assert fault["location"] == "r4 -> exit"
    assert fault["load"] == 120
    assert fault["capacity"] == 100


def test_engine_enumerates_all_combinations() -> None:
    plan = make_plan()
    result = evaluate(plan, confirmed=set(), uncertain={"r1", "r2", "r3"})
    assert result["scenarios_evaluated"] == 8


# ---------- API 全流程 ----------

@pytest.fixture(autouse=True)
def _reset_store() -> None:
    store._prefixes.clear()
    yield
    store._prefixes.clear()


def test_api_full_flow_approve_then_reject_then_receipt_then_approve() -> None:
    prefix = "10.11.0.0/16"
    plan = make_plan(prefix=prefix)
    resp = client.put("/plans", json=plan.model_dump())
    assert resp.status_code == 200

    # 第一批 r3：两种状态（old r4 / new exit）均安全 => 放行，进入 pending
    resp = client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["scenarios_evaluated"] == 2

    # 第二批 r4：与 pending 的 r3 联合穷举，r4=new & r3=old 时 r3<->r4 环路 => 整批退回
    resp = client.post(p(prefix, "applications"), json={"devices": ["r4"]})
    body = resp.json()
    assert resp.status_code == 200
    assert body["decision"] == "rejected"
    assert body["failing_scenarios_count"] >= 1
    assert body["fault_summary"]["环路"] >= 1
    # 原放行记录保留：r3 仍在 pending
    detail = client.get(q(prefix)).json()
    assert detail["pending_devices"] == ["r3"]
    assert detail["confirmed_devices"] == []
    assert len(detail["applications"]) == 2

    # 登记 r3 回执 => r3 进入 confirmed，强制新路由 r3->exit
    resp = client.post(p(prefix, "receipts"), json={"device": "r3"})
    assert resp.status_code == 200
    assert resp.json()["confirmed_devices"] == ["r3"]

    # 重新申请 r4：r3 已确认新路由，r4 新旧两种状态都能到出口 => 放行
    resp = client.post(p(prefix, "applications"), json={"devices": ["r4"]})
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["failing_scenarios_count"] == 0


def test_api_rejects_duplicate_and_unknown_devices() -> None:
    prefix = "10.12.0.0/16"
    client.put("/plans", json=make_plan(prefix=prefix).model_dump())
    client.post(p(prefix, "applications"), json={"devices": ["r3"]})

    # 重复申请 pending 设备 => 409
    resp = client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    assert resp.status_code == 409

    # 未知设备 => 400
    resp = client.post(p(prefix, "applications"), json={"devices": ["nope"]})
    assert resp.status_code == 400

    # 未放行设备登记回执 => 409
    resp = client.post(p(prefix, "receipts"), json={"device": "r1"})
    assert resp.status_code == 409

    # 回执幂等
    client.post(p(prefix, "receipts"), json={"device": "r3"})
    resp = client.post(p(prefix, "receipts"), json={"device": "r3"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_registered"


def test_api_receipt_changes_decision_concurrency() -> None:
    """并发申请按裁决时刻已登记的批次/回执串行判断。"""
    prefix = "10.13.0.0/16"
    plan = PlanIn(
        prefix=prefix,
        exit_node="exit",
        routers=["r1", "r2", "r3"],
        links=[
            {"src": "r1", "dst": "r2", "capacity": 100},
            {"src": "r1", "dst": "r3", "capacity": 100},
            {"src": "r2", "dst": "r3", "capacity": 100},
            {"src": "r3", "dst": "exit", "capacity": 100},
        ],
        ingresses=[{"node": "r1", "traffic": 10}],
        routes=[
            {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
            {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r1"},
            {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
        ],
    )
    client.put("/plans", json=plan.model_dump())

    async def two_concurrent_applications() -> list[str]:
        # 两个申请各自单独都安全；同时在途时，后裁决者必须把先裁决的批次算进去
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(base_url="http://test", transport=transport) as ac:
            url = f"/plans/applications?prefix={quote(prefix, safe='')}"
            results = await asyncio.gather(
                ac.post(url, json={"devices": ["r2"]}),
                ac.post(url, json={"devices": ["r1"]}),
            )
        return [r.json()["decision"] for r in results]

    decisions = asyncio.run(two_concurrent_applications())
    assert sorted(decisions) == ["approved", "rejected"]

    detail = client.get(q(prefix)).json()
    # 只有先裁决的批次进入 pending，退回的批次不留放行记录（具体是哪台由调度顺序决定）
    assert set(detail["pending_devices"]) in ({"r1"}, {"r2"})
    assert len(detail["pending_devices"]) == 1


def test_api_overload_and_open_circuit_locations() -> None:
    prefix = "10.14.0.0/16"
    plan = PlanIn(
        prefix=prefix,
        exit_node="exit",
        routers=["r1", "r2", "r3"],
        links=[
            {"src": "r1", "dst": "r2", "capacity": 100},
            {"src": "r2", "dst": "r3", "capacity": 150},
            {"src": "r3", "dst": "exit", "capacity": 100},
        ],
        ingresses=[{"node": "r1", "traffic": 60}, {"node": "r2", "traffic": 60}],
        routes=[
            # r1 新路由指向 r3，但没有 r1->r3 链路 => 断路
            {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
            {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r3"},
            {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
        ],
    )
    client.put("/plans", json=plan.model_dump())
    resp = client.post(p(prefix, "applications"), json={"devices": ["r1"]})
    body = resp.json()
    assert body["decision"] == "rejected"
    types = {f["type"] for s in body["failing_scenarios"] for f in s["faults"]}
    assert {"断路", "超载"} <= types
    locations = {f["location"] for s in body["failing_scenarios"] for f in s["faults"]}
    assert "r1 -> r3" in locations
    assert "r3 -> exit" in locations


def test_api_put_replaces_and_resets_state() -> None:
    prefix = "10.15.0.0/16"
    client.put("/plans", json=make_plan(prefix=prefix).model_dump())
    client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    client.post(p(prefix, "receipts"), json={"device": "r3"})
    detail = client.get(q(prefix)).json()
    assert detail["confirmed_devices"] == ["r3"]

    # 同前缀重新 PUT => 全量覆盖，放行状态与申请历史重置
    client.put("/plans", json=make_plan(prefix=prefix).model_dump())
    detail = client.get(q(prefix)).json()
    assert detail["confirmed_devices"] == []
    assert detail["pending_devices"] == []
    assert detail["application_count"] == 0


def test_api_unknown_prefix_returns_404() -> None:
    assert client.get(q("9.9.9.0/24")).status_code == 404
    resp = client.post(p("9.9.9.0/24", "applications"), json={"devices": ["r1"]})
    assert resp.status_code == 404
    resp = client.post(p("9.9.9.0/24", "receipts"), json={"device": "r1"})
    assert resp.status_code == 404
