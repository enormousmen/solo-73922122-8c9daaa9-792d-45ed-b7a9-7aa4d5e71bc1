#!/usr/bin/env bash
# 回执到达前后放行结果不同的 API 示例（curl 版），并对照状态缓存的复用与失效
# 前置：docker compose up -d  或  uvicorn app.main:app --port 8000
#
# 放行接口响应头：
#   X-States-Computed  本次实际核算（缓存未命中、逐跳追踪）的状态数
#   X-Cache-Hits       本次命中进程内缓存的状态数
#   X-Cache-Size       请求结束后进程内缓存的状态结果份数（上限 256，LRU 淘汰）
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PREFIX="10.20.0.0%2F16"   # 10.20.0.0/16，'/' 做百分号编码后走 query 参数

echo "== 1. 录入换路方案 =="
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d "$(sed 's/10.11.0.0\/16/10.20.0.0\/16/' "$(dirname "$0")/plan_a.json")"
echo

echo "== 2. 申请 r3 放行（预期 approved；2 个状态全部实际核算：X-States-Computed: 2）=="
curl -s -D - -o /tmp/resp2.json -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}' | grep -iE 'X-States-Computed|X-Cache-'
cat /tmp/resp2.json; echo

echo "== 3. 申请 r4 放行（预期 rejected：r3=old & r4=new 时 r3<->r4 环路）=="
echo "   4 个状态中 2 个与第 2 步核算过的完整状态一致 => X-Cache-Hits: 2, X-States-Computed: 2"
curl -s -D - -o /tmp/resp3.json -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}' | grep -iE 'X-States-Computed|X-Cache-'
cat /tmp/resp3.json; echo

echo "== 4. 登记 r3 成功回执（此后 r3 按新路由 r3->exit 核算；不清缓存）=="
curl -s -X POST "$BASE/plans/receipts?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
echo

echo "== 5. 重新申请 r4（预期 approved；2 个状态均已核算过 => X-Cache-Hits: 2, X-States-Computed: 0）=="
curl -s -D - -o /tmp/resp5.json -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}' | grep -iE 'X-States-Computed|X-Cache-'
cat /tmp/resp5.json; echo

echo "== 6. 全量覆盖同一网段方案（plan_version 递增并清空该网段缓存）=="
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d "$(sed 's/10.11.0.0\/16/10.20.0.0\/16/' "$(dirname "$0")/plan_a.json")"
echo

echo "== 7. 覆盖后重新申请 r3（必须按新方案重新核算 => X-Cache-Hits: 0, X-States-Computed: 2）=="
curl -s -D - -o /tmp/resp7.json -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}' | grep -iE 'X-States-Computed|X-Cache-'
cat /tmp/resp7.json; echo

echo "== 8. 查询网段全量状态（含全部申请历史）=="
curl -s "$BASE/plans/detail?prefix=$PREFIX"
echo
