#!/bin/bash
# E3：overdraft 削长尾——legacy cap 不动，仅等待超 1s 的重读允许超发 2 条。
# abc_90 与 random_90 各一轮 tfs-a01。
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/wsl_run.sh
echo "=== E3 abc_90 overdraft start $(date +%T)"
bash "$W" formal od-abc mooncake_abc_360_4s_12_90GBps tfs-a01 '{"overdraft_wait_s":1.0,"overdraft_max_extra":2}'
echo "=== E3 random_90 overdraft start $(date +%T)"
bash "$W" formal od-random mooncake_random_360_4s_12_90GBps tfs-a01 '{"overdraft_wait_s":1.0,"overdraft_max_extra":2}'
echo "ALL_DONE $(date +%T)"
