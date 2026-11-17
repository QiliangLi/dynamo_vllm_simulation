#!/bin/bash
# E1+E2：abc_90 fixed cap 扫描（6/9/12/18）+ measured 探索轮。串行执行。
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/wsl_run.sh
CASE=mooncake_abc_360_4s_12_90GBps
for CAP in 6 9 12 18; do
  echo "=== E1 fixed$CAP start $(date +%T)"
  bash "$W" formal "fx$CAP" "$CASE" tfs-a01 "{\"cap_policy\":\"fixed\",\"cap_fixed_int\":$CAP}"
done
echo "=== E2 measured start $(date +%T)"
bash "$W" formal measured "$CASE" tfs-a01 '{"cap_policy":"measured"}'
echo "ALL_DONE $(date +%T)"
