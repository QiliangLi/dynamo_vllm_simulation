#!/bin/bash
# E6：多盘复现与修复验证——abc_90 三盘×30GB/s（总带宽不变），四轮：
#   direct 对照 / tfs-a01 legacy / tfs-a01 measured / tfs-a01 overdraft。
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/wsl_run.sh
CASE=mooncake_abc_360_4s_12_90GBps
D="--disks 3 --disk-gbps 30"
echo "=== E6 md-direct start $(date +%T)"
bash "$W" formal md-direct "$CASE" direct - $D
echo "=== E6 md-legacy start $(date +%T)"
bash "$W" formal md-legacy "$CASE" tfs-a01 - $D
echo "=== E6 md-measured start $(date +%T)"
bash "$W" formal md-measured "$CASE" tfs-a01 '{"cap_policy":"measured"}' $D
echo "=== E6 md-od start $(date +%T)"
bash "$W" formal md-od "$CASE" tfs-a01 '{"overdraft_wait_s":1.0,"overdraft_max_extra":2}' $D
echo "ALL_DONE $(date +%T)"
