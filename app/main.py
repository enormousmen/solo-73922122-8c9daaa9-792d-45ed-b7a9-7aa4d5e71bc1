"""FastAPI 路由：换路方案录入、放行裁决、回执登记与状态查询。"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException

from .engine import MAX_REPORTED_SCENARIOS, evaluate
from .models import (
    ApplicationIn,
    ApplicationOut,
    PlanDetail,
    PlanIn,
    PlanSummary,
    ReceiptIn,
    ReceiptOut,
    ScenarioOut,
)
from .storage import store, utc_now_iso

app = FastAPI(
    title="机房换路放行服务",
    version="1.0.0",
    description=(
        "值班员录入目的网段的出口、路由器新旧下一跳、入口流量与有向链路容量，"
        "按批申请下发新配置。服务端穷举本批设备与已放行未确认设备的所有新旧"
        "组合，保证任意切换次序下流量均可到达出口且链路不超载；有风险则整批退回。"
    ),
)


def _summary(state) -> PlanSummary:
    return PlanSummary(
        prefix=state.plan.prefix,
        exit_node=state.plan.exit_node,
        routers=list(state.plan.routers),
        confirmed_devices=sorted(state.confirmed),
        pending_devices=sorted(state.pending),
        application_count=len(state.applications),
        updated_at=state.updated_at,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.put("/plans", response_model=PlanSummary)
async def put_plan(plan: PlanIn) -> PlanSummary:
    """录入或全量覆盖一个目的网段的换路方案。"""
    async with store.lock:
        state = await store.put_plan(plan)
        return _summary(state)


@app.get("/plans", response_model=list[PlanSummary])
async def list_plans() -> list[PlanSummary]:
    async with store.lock:
        return [_summary(state) for state in await store.list_prefixes()]


@app.get("/plans/detail", response_model=PlanDetail)
async def get_plan(prefix: str) -> PlanDetail:
    async with store.lock:
        try:
            state = await store.require(prefix)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")
        return PlanDetail(
            **_summary(state).model_dump(),
            plan=state.plan,
            applications=list(state.applications),
            created_at=state.created_at,
        )


@app.post("/plans/applications", response_model=ApplicationOut)
async def apply(prefix: str, body: ApplicationIn) -> ApplicationOut:
    """申请一批设备下发新配置；整批通过或整批退回。"""
    async with store.lock:
        try:
            state = await store.require(prefix)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")

        routers = set(state.plan.routers)
        unknown = [d for d in body.devices if d not in routers]
        if unknown:
            raise HTTPException(status_code=400, detail=f"设备不属于该网段: {unknown}")

        batch = set(body.devices)
        conflict_pending = sorted(batch & state.pending)
        conflict_confirmed = sorted(batch & state.confirmed)
        if conflict_pending or conflict_confirmed:
            raise HTTPException(
                status_code=409,
                detail=(
                    "存在已放行设备，不能重复申请；"
                    f"待确认={conflict_pending}，已确认={conflict_confirmed}。"
                    "待确认设备请先登记成功回执"
                ),
            )

        # 不确定设备 = 本批 ∪ 已放行未确认；已确认设备强制新路由
        uncertain = batch | state.pending
        result = evaluate(state.plan, confirmed=set(state.confirmed), uncertain=uncertain)

        application_id = f"app-{uuid.uuid4().hex[:12]}"
        decided_at = utc_now_iso()
        failing_report = result["failing_scenarios"][:MAX_REPORTED_SCENARIOS]
        truncated = result["failing_scenarios_count"] > len(failing_report)

        if result["approved"]:
            decision = "approved"
            message = (
                f"全部 {result['scenarios_evaluated']} 种新旧组合核算通过，"
                f"本批 {len(batch)} 台设备放行；登记成功回执前按不确定设备处理"
            )
            # 放行：本批进入“已放行未确认”，原放行记录保持不变
            state.pending |= batch
        else:
            decision = "rejected"
            message = (
                f"在 {result['failing_scenarios_count']}/{result['scenarios_evaluated']} "
                "种新旧组合下存在断路/环路/超载，整批退回，原有放行记录不变"
            )

        out = ApplicationOut(
            application_id=application_id,
            prefix=prefix,
            decision=decision,
            batch=sorted(batch),
            uncertain_devices=sorted(uncertain),
            scenarios_evaluated=result["scenarios_evaluated"],
            failing_scenarios_count=result["failing_scenarios_count"],
            fault_summary=result["fault_summary"],
            failing_scenarios=[ScenarioOut(**s) for s in failing_report],
            failing_scenarios_truncated=truncated,
            message=message,
            decided_at=decided_at,
        )
        # 退回记录同样留痕，便于审计“保留原放行记录”的裁决过程
        state.applications.append(out)
        state.updated_at = decided_at
        return out


@app.post("/plans/receipts", response_model=ReceiptOut)
async def register_receipt(prefix: str, body: ReceiptIn) -> ReceiptOut:
    """登记已放行设备的成功回执；之后该设备按新路由参与核算。"""
    async with store.lock:
        try:
            state = await store.require(prefix)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"目的网段 {prefix} 尚未录入方案")

        if body.device not in state.plan.routers:
            raise HTTPException(status_code=400, detail=f"设备 {body.device} 不属于该网段")

        recorded_at = utc_now_iso()
        if body.device in state.confirmed:
            # 幂等：重复回执不改变状态
            return ReceiptOut(
                prefix=prefix,
                device=body.device,
                receipt_id=state.receipts[body.device],
                status="already_registered",
                confirmed_devices=sorted(state.confirmed),
                pending_devices=sorted(state.pending),
                recorded_at=recorded_at,
            )

        if body.device not in state.pending:
            raise HTTPException(
                status_code=409,
                detail=f"设备 {body.device} 尚未放行，不能登记回执",
            )

        receipt_id = body.receipt_id or f"rcpt-{uuid.uuid4().hex[:12]}"
        state.pending.discard(body.device)
        state.confirmed.add(body.device)
        state.receipts[body.device] = receipt_id
        state.updated_at = recorded_at
        return ReceiptOut(
            prefix=prefix,
            device=body.device,
            receipt_id=receipt_id,
            status="registered",
            confirmed_devices=sorted(state.confirmed),
            pending_devices=sorted(state.pending),
            recorded_at=recorded_at,
        )
