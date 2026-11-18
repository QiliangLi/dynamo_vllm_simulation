#!/bin/bash
# 全量同步 Windows 工作树到 WSL /root/dv-repo（含服务器构建源与工具）。
set -e
SRC=/mnt/d/Coding/dynamo_vllm_simulation
DST=/root/dv-repo
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'upstream' --exclude 'build' \
  --exclude 'results' --exclude '__pycache__' --exclude '.tmp_calib' \
  --exclude 'dynamo_vllm_cpu_sim/RUNTIME_SYNC.json' \
  "$SRC/dynamo_vllm_cpu_sim/sim" "$DST/dynamo_vllm_cpu_sim/"
rsync -a --delete --exclude '__pycache__' \
  "$SRC/dynamo_vllm_cpu_sim/scripts" "$SRC/dynamo_vllm_cpu_sim/configs" \
  "$SRC/dynamo_vllm_cpu_sim/tests" "$SRC/dynamo_vllm_cpu_sim/patched" \
  "$DST/dynamo_vllm_cpu_sim/"
rsync -a --delete \
  "$SRC/benchmark" "$SRC/benchmark_tools_20261116" "$SRC/source_dev" \
  "$DST/"
echo SYNC_ALL_OK
