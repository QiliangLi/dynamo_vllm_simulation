#!/bin/bash
# E4：random_90 矩阵——legacy 对照 + fixed 8/12/18 + measured。串行。
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/wsl_run.sh
CASE=mooncake_random_360_4s_12_90GBps
echo "=== E4 legacy (no BENCH_TFS_JSON) start $(date +%T)"
bash "$W" formal rnd-legacy "$CASE" tfs-a01 -
for CAP in 8 12 18; do
  echo "=== E4 fixed$CAP start $(date +%T)"
  bash "$W" formal "rnd-fx$CAP" "$CASE" tfs-a01 "{\"cap_policy\":\"fixed\",\"cap_fixed_int\":$CAP}"
done
echo "=== E4 measured start $(date +%T)"
bash "$W" formal rnd-measured "$CASE" tfs-a01 '{"cap_policy":"measured"}'
echo "ALL_DONE $(date +%T)"
