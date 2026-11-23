"""状态结果缓存的自测：复用正确性、LRU 容量、回执/覆盖语义与 API 响应头。"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.cache import MAX_CACHED_STATES, StateKey, StateResultCache, state_cache
from app.engine import evaluate
from app.main import app
from app.models import PlanIn
from app.storage import store

client = TestClient(app)


def p(path_prefix: str, suffix: str) -> str:
    return f"/plans/{suffix}?prefix={quote(path_prefix, safe='')}"


def q(path_prefix: str) -> str:
    return f"/plans/detail?prefix={quote(path_prefix, safe='')}"


def make_plan(prefix: str = "10.10.0.0/16") -> PlanIn:
    # 与主测试套件相同的 4 路由器演示拓扑
    return PlanIn(
        prefix=prefix,
        exit_node="exit",
        routers=["r1", "r2", "r3", "r4"],
        links=[
            {"src": "r1", "dst": "r2", "capacity": 100},
            {"src": "r1", "dst": "r3", "capacity": 100},
            {"src": "r2", "dst": "r4", "capacity": 100},
            {"src": "r2", "dst": "r1", "capacity": 100},
            {"src": "r3", "dst": "r4", "capacity": 100},
            {"src": "r3", "dst": "exit", "capacity": 100},
            {"src": "r4", "dst": "exit", "capacity": 100},
            {"src": "r4", "dst": "r3", "capacity": 100},
        ],
        ingresses=[{"node": "r1", "traffic": 10}],
        routes=[
            {"device": "r1", "old_next_hop": "r2", "new_next_hop": "r3"},
            {"device": "r2", "old_next_hop": "r4", "new_next_hop": "r1"},
            {"device": "r3", "old_next_hop": "r4", "new_next_hop": "exit"},
            {"device": "r4", "old_next_hop": "exit", "new_next_hop": "r3"},
        ],
    )


# 结果中需要与“全量核算”逐字一致的字段
RESULT_FIELDS = (
    "approved",
    "scenarios_evaluated",
    "failing_scenarios_count",
    "fault_summary",
    "scenarios",
)


@pytest.fixture(autouse=True)
def _reset_store_and_cache() -> None:
    store._prefixes.clear()
    state_cache.clear()
    yield
    store._prefixes.clear()
    state_cache.clear()


def _full_eval(plan: PlanIn, confirmed: set[str], uncertain: set[str]) -> dict:
    """不走缓存的全量核算，作为对照基线。"""
    return evaluate(plan, confirmed=confirmed, uncertain=uncertain, cache=None)


# ---------- 引擎层：缓存命中与全量核算逐字一致 ----------

def test_cached_evaluation_matches_full_evaluation() -> None:
    plan = make_plan()
    uncertain = {"r3", "r4"}

    first = evaluate(plan, confirmed=set(), uncertain=uncertain,
                     prefix=plan.prefix, plan_version=1, cache=state_cache)
    # 首次：两个状态全部实际核算，缓存为空
    assert first["states_computed"] == 4
    assert first["cache_hits"] == 0
    assert len(state_cache) == 4

    baseline = _full_eval(plan, confirmed=set(), uncertain=uncertain)
    for field in RESULT_FIELDS:
        assert first[field] == baseline[field]

    # 再次核算完全相同的状态集合：全部命中，不再实际核算，结论逐字一致
    second = evaluate(plan, confirmed=set(), uncertain=uncertain,
                      prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert second["states_computed"] == 0
    assert second["cache_hits"] == 4
    for field in RESULT_FIELDS:
        assert second[field] == baseline[field]


def test_partial_hit_reuses_cached_and_computes_missing() -> None:
    """本轮需要且开算时已缓存的状态都要复用，未命中的状态继续核算。"""
    plan = make_plan()

    # 先核算不确定集合 {r4}：缓存 r4=old / r4=new 两份完整状态
    small = evaluate(plan, confirmed=set(), uncertain={"r4"},
                     prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert small["states_computed"] == 2
    assert len(state_cache) == 2

    # 扩大为 {r3, r4}：共 4 个状态，其中 2 个（r3 固定 old 的两份）应命中
    merged = evaluate(plan, confirmed=set(), uncertain={"r3", "r4"},
                      prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert merged["scenarios_evaluated"] == 4
    assert merged["cache_hits"] == 2
    assert merged["states_computed"] == 2
    assert len(state_cache) == 4

    baseline = _full_eval(plan, confirmed=set(), uncertain={"r3", "r4"})
    for field in RESULT_FIELDS:
        assert merged[field] == baseline[field]


def test_receipt_keeps_reusing_matching_states() -> None:
    """登记回执只改变哪些完整状态会出现；吻合的历史结果继续复用。"""
    plan = make_plan()

    before = evaluate(plan, confirmed=set(), uncertain={"r3", "r4"},
                      prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert before["states_computed"] == 4

    # r3 登记回执 => r3 强制 new；不确定集合只剩 r4。
    # 两个完整状态 (r3=new, r4=old/new) 此前都核算过，应全部命中
    after = evaluate(plan, confirmed={"r3"}, uncertain={"r4"},
                     prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert after["cache_hits"] == 2
    assert after["states_computed"] == 0
    assert after["approved"] is True

    baseline = _full_eval(plan, confirmed={"r3"}, uncertain={"r4"})
    for field in RESULT_FIELDS:
        assert after[field] == baseline[field]


def test_same_partial_assignment_is_not_same_state() -> None:
    """只有方案内每台设备的新旧选择都一致才算同一状态。"""
    plan = make_plan()

    # {r4} 穷举出的 r4=old 状态里 r3 也是 old
    evaluate(plan, confirmed=set(), uncertain={"r4"},
             prefix=plan.prefix, plan_version=1, cache=state_cache)
    size_after_first = len(state_cache)

    # r3 回执（强制 new）后再看 r4=old：r4 选择相同但 r3 不同 => 新状态，须重新核算
    result = evaluate(plan, confirmed={"r3"}, uncertain={"r4"},
                      prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert result["states_computed"] == 2
    assert result["cache_hits"] == 0
    assert len(state_cache) == size_after_first + 2


def test_plan_version_change_forces_full_recompute() -> None:
    """覆盖方案后版本递增：旧版本缓存永不命中。"""
    plan = make_plan()
    evaluate(plan, confirmed=set(), uncertain={"r3", "r4"},
             prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert len(state_cache) == 4

    # 相同拓扑但新版本号 => 键不同，全部重新核算
    result = evaluate(plan, confirmed=set(), uncertain={"r3", "r4"},
                      prefix=plan.prefix, plan_version=2, cache=state_cache)
    assert result["states_computed"] == 4
    assert result["cache_hits"] == 0
    # 新旧版本条目共存（API 覆盖时会主动 invalidate_prefix 清理旧条目）
    assert len(state_cache) == 8


# ---------- LRU 容量与淘汰 ----------

def test_lru_cache_caps_at_256_and_evicts_least_recently_used() -> None:
    cache = StateResultCache(maxsize=MAX_CACHED_STATES)
    assert MAX_CACHED_STATES == 256

    for i in range(MAX_CACHED_STATES):
        cache.put(
            StateKey("pfx", 1, (f"s{i}",)),
            {"faults": [{"index": i}]},
        )
    assert len(cache) == MAX_CACHED_STATES

    # 访问第 0 条，把它刷新为最近使用
    assert cache.get(StateKey("pfx", 1, ("s0",))) == {"faults": [{"index": 0}]}

    # 再写入一条 => 淘汰最久未用的 s1（s0 刚被访问，应保留）
    cache.put(StateKey("pfx", 1, ("s_new",)), {"faults": []})
    assert len(cache) == MAX_CACHED_STATES
    assert cache.get(StateKey("pfx", 1, ("s1",))) is None
    assert cache.get(StateKey("pfx", 1, ("s0",))) == {"faults": [{"index": 0}]}
    assert cache.get(StateKey("pfx", 1, ("s_new",))) == {"faults": []}


def test_invalidate_prefix_only_clears_matching_prefix() -> None:
    cache = StateResultCache()
    cache.put(StateKey("a", 1, ("old",)), {"faults": []})
    cache.put(StateKey("b", 1, ("old",)), {"faults": []})

    removed = cache.invalidate_prefix("a")
    assert removed == 1
    assert cache.get(StateKey("a", 1, ("old",))) is None
    assert cache.get(StateKey("b", 1, ("old",))) == {"faults": []}


def test_global_cache_never_exceeds_256_through_engine() -> None:
    """12 台路由器 4096 状态远超容量：缓存始终 <= 256，且结果仍与全量核算一致。"""
    routers = [f"r{i}" for i in range(12)]
    plan = PlanIn(
        prefix="10.99.0.0/16",
        exit_node="exit",
        routers=routers,
        links=[{"src": r, "dst": "exit", "capacity": 100} for r in routers],
        ingresses=[{"node": routers[0], "traffic": 1}],
        routes=[
            {"device": r, "old_next_hop": "exit", "new_next_hop": "exit"}
            for r in routers
        ],
    )

    first = evaluate(plan, confirmed=set(), uncertain=set(routers),
                     prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert first["scenarios_evaluated"] == 4096
    assert first["states_computed"] == 4096
    assert len(state_cache) == MAX_CACHED_STATES  # 写满后 LRU 淘汰，不超限

    # 工作集（4096）远大于容量（256），第二次穷举全部未命中，但结论必须仍正确
    second = evaluate(plan, confirmed=set(), uncertain=set(routers),
                      prefix=plan.prefix, plan_version=1, cache=state_cache)
    assert second["cache_hits"] == 0
    assert second["states_computed"] == 4096
    assert len(state_cache) == MAX_CACHED_STATES

    baseline = _full_eval(plan, confirmed=set(), uncertain=set(routers))
    assert second["approved"] == baseline["approved"]
    assert second["scenarios"] == baseline["scenarios"]


# ---------- API 层：响应头、历史一致性、覆盖失效 ----------

def test_randomized_cache_output_equals_full_evaluation() -> None:
    """随机多组 confirmed/uncertain 组合：缓存路径与全量核算输出逐字段一致。"""
    import random

    rng = random.Random(20261123)
    plan = make_plan()
    routers = sorted(plan.routers)

    for _ in range(25):
        confirmed = set(rng.sample(routers, rng.randrange(0, 3)))
        remaining = [r for r in routers if r not in confirmed]
        uncertain = set(rng.sample(remaining, rng.randrange(0, len(remaining) + 1)))

        cached_result = evaluate(
            plan, confirmed=confirmed, uncertain=uncertain,
            prefix=plan.prefix, plan_version=1, cache=state_cache,
        )
        full_result = _full_eval(plan, confirmed=confirmed, uncertain=uncertain)
        for field in RESULT_FIELDS:
            assert cached_result[field] == full_result[field]
        # 命中 + 实际核算恒等于穷举场景总数
        assert (
            cached_result["cache_hits"] + cached_result["states_computed"]
            == cached_result["scenarios_evaluated"]
        )


def test_api_headers_report_computed_states_and_hits() -> None:
    prefix = "10.30.0.0/16"
    client.put("/plans", json=make_plan(prefix=prefix).model_dump())

    # 首次申请 r3：2 个状态全部实际核算
    resp = client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    assert resp.status_code == 200
    assert resp.headers["X-States-Computed"] == "2"
    assert resp.headers["X-Cache-Hits"] == "0"
    assert int(resp.headers["X-Cache-Size"]) == 2

    # 退回型申请 r4：4 个状态中 r3=old 的两份此前核算过（r3 仍 pending=old/new 穷举，
    # 其中 r3=old 两状态与 r3 单独申请时的完整状态一致）=> 2 命中 2 新核算
    resp = client.post(p(prefix, "applications"), json={"devices": ["r4"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "rejected"
    assert resp.headers["X-Cache-Hits"] == "2"
    assert resp.headers["X-States-Computed"] == "2"
    assert int(resp.headers["X-Cache-Size"]) == 4

    # 与无缓存全量核算对照：裁决、统计、故障场景（编号/顺序/内容）完全一致
    state = store._prefixes[prefix]
    baseline = _full_eval(state.plan, confirmed=set(), uncertain={"r3", "r4"})
    assert body["decision"] == ("approved" if baseline["approved"] else "rejected")
    assert body["scenarios_evaluated"] == baseline["scenarios_evaluated"]
    assert body["failing_scenarios_count"] == baseline["failing_scenarios_count"]
    assert body["fault_summary"] == baseline["fault_summary"]

    def _normalize(scenario: dict) -> dict:
        # API 响应经 Pydantic 序列化后故障里恒带 load/capacity（非超载为 None），
        # 引擎原始字典只在超载时带这两个键；比对时去掉 None 字段
        return {
            "index": scenario["index"],
            "device_states": scenario["device_states"],
            "reachable": scenario["reachable"],
            "faults": [
                {k: v for k, v in fault.items() if v is not None or k not in ("load", "capacity")}
                for fault in scenario["faults"]
            ],
        }

    reported = [_normalize(s) for s in body["failing_scenarios"]]
    assert reported == baseline["failing_scenarios"]


def test_api_history_identical_to_full_evaluation() -> None:
    """申请响应与历史中的放行结论、统计和故障示例与全量核算一致，场景编号顺序也一样。"""
    prefix = "10.31.0.0/16"
    client.put("/plans", json=make_plan(prefix=prefix).model_dump())
    client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    client.post(p(prefix, "applications"), json={"devices": ["r4"]})
    client.post(p(prefix, "receipts"), json={"device": "r3"})
    client.post(p(prefix, "applications"), json={"devices": ["r4"]})

    detail = client.get(q(prefix)).json()
    state = store._prefixes[prefix]

    # 用无缓存全量核算逐条重放历史申请，逐字段对照
    expected_specs = [
        (set(), set(), {"r3"}),
        (set(), {"r3"}, {"r4"}),
        ({"r3"}, set(), {"r4"}),
    ]
    assert len(detail["applications"]) == 3
    for app_record, (confirmed, pending, batch) in zip(detail["applications"], expected_specs):
        baseline = _full_eval(state.plan, confirmed=confirmed, uncertain=pending | batch)
        assert app_record["decision"] == ("approved" if baseline["approved"] else "rejected")
        assert app_record["scenarios_evaluated"] == baseline["scenarios_evaluated"]
        assert app_record["failing_scenarios_count"] == baseline["failing_scenarios_count"]
        assert app_record["fault_summary"] == baseline["fault_summary"]
        assert app_record["failing_scenarios_truncated"] == (
            baseline["failing_scenarios_count"] > len(baseline["failing_scenarios"][:100])
        )
        expected_report = [
            {
                "index": s["index"],
                "device_states": s["device_states"],
                "reachable": s["reachable"],
                "faults": s["faults"],
            }
            for s in baseline["failing_scenarios"][:100]
        ]
        actual_report = [
            {
                "index": s["index"],
                "device_states": s["device_states"],
                "reachable": s["reachable"],
                "faults": [
                    {k: v for k, v in f.items() if v is not None or k not in ("load", "capacity")}
                    for f in s["faults"]
                ],
            }
            for s in app_record["failing_scenarios"]
        ]
        assert actual_report == expected_report


def test_api_plan_overwrite_invalidates_cache_and_recomputes() -> None:
    prefix = "10.32.0.0/16"
    plan = make_plan(prefix=prefix)
    client.put("/plans", json=plan.model_dump())
    client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    assert len(state_cache) == 2
    old_version = store._prefixes[prefix].plan_version
    assert old_version == 1

    # 另一个网段的缓存条目应在覆盖后保留
    other = make_plan(prefix="10.33.0.0/16")
    client.put("/plans", json=other.model_dump())
    client.post(p(other.prefix, "applications"), json={"devices": ["r3"]})
    other_entries_before = [k for k in state_cache._entries if k.prefix == other.prefix]
    assert len(other_entries_before) == 2

    # 同前缀全量覆盖 => 版本递增、该网段缓存清空
    client.put("/plans", json=plan.model_dump())
    state = store._prefixes[prefix]
    assert state.plan_version == old_version + 1
    assert not [k for k in state_cache._entries if k.prefix == prefix]
    # 其它网段缓存不受影响
    assert [k for k in state_cache._entries if k.prefix == other.prefix] == other_entries_before

    # 覆盖后重新申请：必须按新方案重新核算（2 个状态全部实际计算）
    resp = client.post(p(prefix, "applications"), json={"devices": ["r3"]})
    assert resp.status_code == 200
    assert resp.headers["X-States-Computed"] == "2"
    assert resp.headers["X-Cache-Hits"] == "0"
    assert store._prefixes[prefix].plan_version == 2


def test_api_repeated_rejected_application_fully_served_from_cache() -> None:
    """同一网段反复申请相同换路（退回不改状态）：第二次起全部命中缓存。"""
    prefix = "10.34.0.0/16"
    client.put("/plans", json=make_plan(prefix=prefix).model_dump())
    client.post(p(prefix, "applications"), json={"devices": ["r3"]})  # 放行，r3 进 pending

    first = client.post(p(prefix, "applications"), json={"devices": ["r4"]})
    assert first.json()["decision"] == "rejected"
    assert first.headers["X-States-Computed"] == "2"
    assert first.headers["X-Cache-Hits"] == "2"

    # 退回不改变放行状态，同样的申请再次提交 => 4 个状态全部已缓存
    second = client.post(p(prefix, "applications"), json={"devices": ["r4"]})
    assert second.json()["decision"] == "rejected"
    assert second.headers["X-States-Computed"] == "0"
    assert second.headers["X-Cache-Hits"] == "4"
    # 两次响应体（除申请编号/时间外）完全一致
    a, b = first.json(), second.json()
    for key in (
        "decision", "batch", "uncertain_devices", "scenarios_evaluated",
        "failing_scenarios_count", "fault_summary", "failing_scenarios",
        "failing_scenarios_truncated",
    ):
        assert a[key] == b[key]
