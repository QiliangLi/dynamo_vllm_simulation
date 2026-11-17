#!/bin/bash
# E5 同轮结论轮：同一晚连续执行，消除跨轮噪声。
#   abc_90：direct / tfs-a01 legacy / tfs-a01 overdraft(1s,+2)
#   random_90：direct / tfs-a01 legacy / tfs-a01 overdraft / tfs-a01 measured
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/wsl_run.sh
OD='{"overdraft_wait_s":1.0,"overdraft_max_extra":2}'
echo "=== E5 abc direct $(date +%T)"
bash "$W" formal e5-abc-direct mooncake_abc_360_4s_12_90GBps direct -
echo "=== E5 abc legacy $(date +%T)"
bash "$W" formal e5-abc-legacy mooncake_abc_360_4s_12_90GBps tfs-a01 -
echo "=== E5 abc overdraft $(date +%T)"
bash "$W" formal e5-abc-od mooncake_abc_360_4s_12_90GBps tfs-a01 "$OD"
echo "=== E5 random direct $(date +%T)"
bash "$W" formal e5-rnd-direct mooncake_random_360_4s_12_90GBps direct -
echo "=== E5 random legacy $(date +%T)"
bash "$W" formal e5-rnd-legacy mooncake_random_360_4s_12_90GBps tfs-a01 -
echo "=== E5 random overdraft $(date +%T)"
bash "$W" formal e5-rnd-od mooncake_random_360_4s_12_90GBps tfs-a01 "$OD"
echo "=== E5 random measured $(date +%T)"
bash "$W" formal e5-rnd-measured mooncake_random_360_4s_12_90GBps tfs-a01 '{"cap_policy":"measured"}'
echo "ALL_DONE $(date +%T)"
