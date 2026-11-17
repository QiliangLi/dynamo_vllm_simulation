#!/bin/bash
# 收尾批：多盘 od 补轮（验收修复后）+ abcd_90 四轮补全场景。
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/wsl_run.sh
OD='{"overdraft_wait_s":1.0,"overdraft_max_extra":2}'
echo "=== md-od retry $(date +%T)"
bash "$W" formal md-od2 mooncake_abc_360_4s_12_90GBps tfs-a01 "$OD" --disks 3 --disk-gbps 30
CASE=mooncake_abcd_360_4s_12_90GBps
echo "=== abcd direct $(date +%T)"
bash "$W" formal e7-abcd-direct "$CASE" direct -
echo "=== abcd legacy $(date +%T)"
bash "$W" formal e7-abcd-legacy "$CASE" tfs-a01 -
echo "=== abcd overdraft $(date +%T)"
bash "$W" formal e7-abcd-od "$CASE" tfs-a01 "$OD"
echo "=== abcd measured $(date +%T)"
bash "$W" formal e7-abcd-measured "$CASE" tfs-a01 '{"cap_policy":"measured"}'
echo "ALL_DONE $(date +%T)"
