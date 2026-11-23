"""端到端演示：启动 uvicorn 后依次演示

场景 A（10.11.0.0/16，环路 + 回执改变裁决）：
  1) 录入 r1~r4 的新旧下一跳、入口流量、有向链路容量
  2) 申请 r3 放行 -> approved（进入“已放行未确认”）
  3) 申请 r4 放行 -> rejected（r3=old & r4=new 时 r3<->r4 环路，整批退回，原记录保留）
  4) 登记 r3 成功回执
  5) 再次申请 r4 -> approved（r3 已按新路由 r3->exit 转发）

场景 B（10.12.0.0/16，断路 + 超载故障定位）：
  申请 r1 -> rejected，返回断路位置 r1 -> r3（无此链路）与超载位置 r3 -> exit

只依赖标准库；完整 JSON 响应写入 demo_output.json。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

OUTPUT_FILE = Path(__file__).resolve().parent.parent / "demo_output.json"


async def call(client: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> dict:
    resp = await client.request(method, path, json=body, timeout=10)
    data = resp.json()
    if resp.status_code >= 400:
        return {"http_status": resp.status_code, **data}
    return data


def plan_url(prefix: str, suffix: str) -> str:
    return f"/plans/{suffix}?prefix={urllib.parse.quote(prefix, safe='')}"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def summarize_decision(resp: dict) -> None:
    print(f"  裁决: {resp['decision']} | 评估场景数: {resp['scenarios_evaluated']} "
          f"| 故障场景数: {resp['failing_scenarios_count']} | 故障统计: {resp['fault_summary']}")
    print(f"  说明: {resp['message']}")
    for scenario in resp["failing_scenarios"][:3]:
        states = ", ".join(f"{d}={s}" for d, s in sorted(scenario["device_states"].items()))
        print(f"  - 故障场景#{scenario['index']} [{states}]")
        for fault in scenario["faults"]:
            extra = ""
            if fault["type"] == "超载":
                extra = f"（流量 {fault['load']} > 容量 {fault['capacity']}）"
            print(f"      {fault['type']} @ {fault['location']}{extra}: {fault['detail']}")
    if resp["failing_scenarios_truncated"]:
        print("  - 其余故障场景已截断（详见接口返回）")


PLAN_A = {
    "prefix": "10.11.0.0/16",
    "exit_node": "exit",
    "routers": ["r1", "r2", "r3", "r4"],
    "links": [
        {"src": "r1", "dst": "r2", "capacity": 100},
        {"src": "r1", "dst": "r3", "capacity": 100},
        {"src": "r2", "dst": "r4", "capacity": 100},
        {"src": "r2", "dst": "r1", "capacity": 100},
        {"src": "r3", "dst": "r4", "capacity": 100},
        {"src": "r3", "dst": "exit", "capacity": 100},
        {"src": "r4", "dst": "exit", "capacity": 100},
        {"src": "r4", "dst": "r3", "capacity": 100},
    ],
    "ingresses": [{"node": "r1", "traffic": 10}],
    "routes": [
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r4", "new_next_hop": "r1"},
        {"device": "r3", "old_next_hop": "r4", "new_next_hop": "exit"},
        {"device": "r4", "old_next_hop": "exit", "new_next_hop": "r3"},
    ],
}

PLAN_B = {
    "prefix": "10.12.0.0/16",
    "exit_node": "exit",
    "routers": ["r1", "r2", "r3"],
    "links": [
        {"src": "r1", "dst": "r2", "capacity": 100},
        {"src": "r2", "dst": "r3", "capacity": 150},
        {"src": "r3", "dst": "exit", "capacity": 100},
    ],
    "ingresses": [
        {"node": "r1", "traffic": 60},
        {"node": "r2", "traffic": 60},
    ],
    "routes": [
        # r1 新下一跳 r3，但未录入 r1->r3 有向链路 => 断路
        {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
        {"device": "r2", "old_next_hop": "r3", "new_next_hop": "r3"},
        {"device": "r3", "old_next_hop": "exit", "new_next_hop": "exit"},
    ],
}


async def main() -> None:
    archive: dict[str, list[dict]] = {"plan_a": [], "plan_b": []}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://route-approval") as client:
        banner("场景 A：录入换路方案（10.11.0.0/16）")
        resp = await call(client, "PUT", "/plans", PLAN_A)
        print(f"  已录入，当前 pending={resp['pending_devices']} confirmed={resp['confirmed_devices']}")
        archive["plan_a"].append({"step": "put_plan", "response": resp})

        banner("场景 A：第一批申请 [r3] —— 预期 approved")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r3"]})
        summarize_decision(resp)
        archive["plan_a"].append({"step": "apply_r3", "response": resp})
        assert resp["decision"] == "approved"

        banner("场景 A：第二批申请 [r4]（r3 已放行未确认）—— 预期 rejected：r3<->r4 环路")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r4"]})
        summarize_decision(resp)
        archive["plan_a"].append({"step": "apply_r4_before_receipt", "response": resp})
        assert resp["decision"] == "rejected"

        state = await call(client, "GET", plan_url(PLAN_A["prefix"], "detail"))
        print(f"  退回后状态（原放行记录保留）: pending={state['pending_devices']} "
              f"confirmed={state['confirmed_devices']} 历史申请数={state['application_count']}")
        archive["plan_a"].append({"step": "state_after_reject", "response": state})
        assert state["pending_devices"] == ["r3"]

        banner("场景 A：登记 r3 成功回执 —— 此后 r3 按新路由 r3->exit 核算")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "receipts"),
                          {"device": "r3", "note": "现场切换成功"})
        print(f"  回执: {resp['receipt_id']} | pending={resp['pending_devices']} "
              f"confirmed={resp['confirmed_devices']}")
        archive["plan_a"].append({"step": "receipt_r3", "response": resp})

        banner("场景 A：回执到达后重新申请 [r4] —— 预期 approved")
        resp = await call(client, "POST", plan_url(PLAN_A["prefix"], "applications"), {"devices": ["r4"]})
        summarize_decision(resp)
        archive["plan_a"].append({"step": "apply_r4_after_receipt", "response": resp})
        assert resp["decision"] == "approved"

        banner("场景 B：录入方案并申请 [r1] —— 预期 rejected：断路 + 超载，带故障位置")
        await call(client, "PUT", "/plans", PLAN_B)
        resp = await call(client, "POST", plan_url(PLAN_B["prefix"], "applications"), {"devices": ["r1"]})
        summarize_decision(resp)
        archive["plan_b"].append({"step": "apply_r1", "response": resp})
        assert resp["decision"] == "rejected"
        locations = {f["location"] for s in resp["failing_scenarios"] for f in s["faults"]}
        assert "r1 -> r3" in locations and "r3 -> exit" in locations

    OUTPUT_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整 JSON 响应已写入: {OUTPUT_FILE}")
    print("演示完成：回执到达前 r4 被退回，回执到达后 r4 放行。")


if __name__ == "__main__":
    asyncio.run(main())
