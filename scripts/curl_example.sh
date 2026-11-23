#!/usr/bin/env bash
# 回执到达前后放行结果不同的 API 示例（curl 版）
# 前置：docker compose up -d  或  uvicorn app.main:app --port 8000
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PREFIX="10.20.0.0%2F16"   # 10.20.0.0/16，'/' 做百分号编码后走 query 参数

echo "== 1. 录入换路方案 =="
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d "$(sed 's/10.11.0.0\/16/10.20.0.0\/16/' "$(dirname "$0")/plan_a.json")"
echo

echo "== 2. 申请 r3 放行（预期 approved，进入已放行未确认）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}'
echo

echo "== 3. 申请 r4 放行（预期 rejected：r3=old & r4=new 时 r3<->r4 环路，整批退回）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 4. 登记 r3 成功回执（此后 r3 按新路由 r3->exit 核算）=="
curl -s -X POST "$BASE/plans/receipts?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
echo

echo "== 5. 重新申请 r4（预期 approved，与第 3 步结果不同）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 6. 查询网段全量状态（含全部申请历史）=="
curl -s "$BASE/plans/detail?prefix=$PREFIX"
echo
