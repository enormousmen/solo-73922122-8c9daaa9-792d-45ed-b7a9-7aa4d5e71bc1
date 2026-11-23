# API 示例：回执到达前后放行结果不同 + 状态缓存复用对照

以下响应均为服务真实输出（完整 JSON 见 `demo_output.json`，可由 `python scripts/demo.py` 重新生成）。

## 状态缓存与响应头

同一网段反复申请换路时，穷举会反复追踪相同的转发路径。服务把**方案内每台设备走新路
还是旧路的一份完整状态**（confirmed 强制 new、不确定设备按穷举位取 old/new、其余设备
保持 old）的核算结果缓存在单进程内，跨请求复用：

- 只有方案内**每台**设备的新旧选择都一致，才算同一状态、才能命中；
- 登记回执不清除缓存：回执只改变之后会出现哪些完整状态，与缓存中吻合的结果继续复用；
- 全量覆盖方案会让该网段 `plan_version` 递增并清空其缓存条目，之后必须按新方案重新核算；
- 缓存为 LRU，单进程跨请求最多保留 **256** 份状态结果。

放行接口（`POST /plans/applications`）通过响应头给出本次的核算/命中情况：

| 响应头 | 含义 |
| --- | --- |
| `X-States-Computed` | 本次**实际核算**（缓存未命中、逐跳追踪）的状态数 |
| `X-Cache-Hits` | 本次命中缓存、直接复用结果的状态数 |
| `X-Cache-Size` | 请求结束后进程内缓存中的状态结果份数 |

两者满足 `X-States-Computed + X-Cache-Hits = scenarios_evaluated`。缓存只跳过重复追踪，
不改变场景编号、枚举顺序、放行结论、故障统计与故障示例——与全量核算逐字一致。

## 拓扑（目的网段 10.11.0.0/16，出口 exit）

录入载荷见 `scripts/plan_a.json`。路由器与新旧下一跳：

| 设备 | 旧下一跳 | 新下一跳 |
| --- | --- | --- |
| r1 | r2 | r3 |
| r2 | r4 | r1 |
| r3 | r4 | **exit** |
| r4 | exit | **r3** |

入口流量：r1 注入 10；所有相关有向链路（含 `r3->exit`、`r4->r3`、`r3->r4`）容量均为 100。

### 1. 录入方案

```bash
curl -X PUT http://localhost:8000/plans \
  -H 'Content-Type: application/json' -d @scripts/plan_a.json
```

```json
{
  "prefix": "10.11.0.0/16",
  "exit_node": "exit",
  "routers": ["r1", "r2", "r3", "r4"],
  "confirmed_devices": [],
  "pending_devices": [],
  "application_count": 0
}
```

### 2. 申请 r3 —— 通过（2 种新旧组合均安全）

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}'
```

响应头：`X-States-Computed: 2`、`X-Cache-Hits: 0`、`X-Cache-Size: 2`（首次申请，两个状态都要实际核算）。

```json
{
  "application_id": "app-b1124b2ab343",
  "decision": "approved",
  "batch": ["r3"],
  "uncertain_devices": ["r3"],
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0},
  "message": "全部 2 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理"
}
```

此后 r3 进入“已放行未确认”（pending）。

### 3. 申请 r4 —— 整批退回（r3 未确认，存在 r3↔r4 环路组合）

不确定设备 = 本批 `{r4}` ∪ pending `{r3}`，共穷举 4 种组合。
当 `r3=old（->r4）` 且 `r4=new（->r3）` 时，r1 的流量沿
`r1 -> r2 -> r4 -> r3 -> r4` 形成环路：

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

响应头：`X-States-Computed: 2`、`X-Cache-Hits: 2`、`X-Cache-Size: 4`。
4 个穷举状态里，`r3=old` 的两份完整状态与第 2 步核算过的状态（r3 单独申请时
r3=old 那一态、其余设备全部 old）逐设备一致，直接复用；`r3=new` 的两份是新状态，
需要实际核算。

```json
{
  "application_id": "app-d843973d934a",
  "decision": "rejected",
  "batch": ["r4"],
  "uncertain_devices": ["r3", "r4"],
  "scenarios_evaluated": 4,
  "failing_scenarios_count": 1,
  "fault_summary": {"断路": 0, "环路": 1, "超载": 0},
  "failing_scenarios": [
    {
      "index": 1,
      "device_states": {"r3": "old", "r4": "new"},
      "reachable": false,
      "faults": [
        {
          "type": "环路",
          "location": "r1 -> r2 -> r4 -> r3 -> r4",
          "detail": "入口 r1 的流量沿 r1 -> r2 -> r4 -> r3 -> r4 形成转发环路，无法到达出口 exit"
        }
      ]
    }
  ],
  "message": "在 1/4 种新旧组合下存在断路/环路/超载，整批退回，原有放行记录不变"
}
```

退回后查询状态，r3 的原放行记录保留：`pending_devices=["r3"]`，申请历史中同时留有
第 2、3 步两条记录。

### 4. 核算期间登记 r3 的成功回执

```bash
curl -X POST 'http://localhost:8000/plans/receipts?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
```

```json
{
  "prefix": "10.11.0.0/16",
  "device": "r3",
  "receipt_id": "rcpt-4e441de29ac7",
  "status": "registered",
  "confirmed_devices": ["r3"],
  "pending_devices": []
}
```

此后 r3 强制按新路由 `r3 -> exit` 转发，不再参与新旧组合穷举。
重复登记同一设备回执返回 `"status": "already_registered"`（幂等）。

### 5. 回执到达后重新申请 r4 —— 通过（与第 3 步裁决不同）

不确定集合只剩 `{r4}`：r4=old 走 `r4 -> exit`；r4=new 走 `r4 -> r3 -> exit`，两种均安全。

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

响应头：`X-States-Computed: 0`、`X-Cache-Hits: 2`、`X-Cache-Size: 4`。
登记回执没有清缓存：r3 强制新路由后出现的两个完整状态（r3=new、r4=old/new），
在第 3 步穷举 `{r3,r4}` 时都已核算过，本次**零实际核算、全部复用**。

```json
{
  "application_id": "app-8722195038e9",
  "decision": "approved",
  "batch": ["r4"],
  "uncertain_devices": ["r4"],
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0},
  "message": "全部 2 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理"
}
```

**同一批设备（r4），回执前退回、回执后放行**，这就是回执改变裁决结果的完整过程。

### 6. 全量覆盖方案后 —— 缓存失效，按新方案重新核算

对同一前缀重新 `PUT /plans`（即使内容相同）：放行状态与申请历史重置，`plan_version`
递增，该网段此前缓存的状态结果被清空。

```bash
curl -X PUT http://localhost:8000/plans \
  -H 'Content-Type: application/json' -d @scripts/plan_a.json

curl -s -D - -o /dev/null -X POST \
  'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}' | grep -iE 'X-States|X-Cache'
```

```text
X-States-Computed: 2
X-Cache-Hits: 0
X-Cache-Size: 2
```

覆盖后第一次申请 0 命中、2 个状态全部重新核算；其它网段的缓存条目不受影响。

### 核算次数对照表（场景 A）

| 步骤 | 申请 | 评估场景数 | 实际核算 `X-States-Computed` | 命中 `X-Cache-Hits` | 裁决 |
| --- | --- | --- | --- | --- | --- |
| 2 | r3（首次） | 2 | 2 | 0 | approved |
| 3 | r4（r3 在途） | 4 | 2 | 2 | rejected |
| 5 | r4（r3 已回执） | 2 | 0 | 2 | approved |
| 6 | r3（覆盖方案后） | 2 | 2 | 0 | approved |

## 附：断路 + 超载的故障定位（10.12.0.0/16）

录入载荷见 `scripts/plan_b.json`：r1 新下一跳 r3 但未录入 `r1 -> r3` 链路（断路）；
r1、r2 各注入 60，r3 -> exit 容量仅 100（超载）。

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.12.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "rejected",
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 2,
  "fault_summary": {"断路": 1, "环路": 0, "超载": 1},
  "failing_scenarios": [
    {
      "index": 0,
      "device_states": {"r1": "old"},
      "faults": [
        {"type": "超载", "location": "r3 -> exit",
         "detail": "链路 r3 -> exit 合计流量 120 超过容量 100",
         "load": 120.0, "capacity": 100.0}
      ]
    },
    {
      "index": 1,
      "device_states": {"r1": "new"},
      "faults": [
        {"type": "断路", "location": "r1 -> r3",
         "detail": "入口 r1 的流量在 r1 -> r3 处无法继续转发到出口 exit"}
      ]
    }
  ]
}
```

## 并发申请语义

所有申请/回执写操作在同一把 asyncio 锁内串行裁决。两个各自安全的批次并发提交时，
先裁决者放行并进入 pending，后裁决者必须把先裁决的批次计入不确定集合——若联合穷举
出现风险则退回（自测 `test_api_receipt_changes_decision_concurrency` 验证：并发结果
恰为一个 approved、一个 rejected，且只有先裁决的设备出现在 pending 中）。
