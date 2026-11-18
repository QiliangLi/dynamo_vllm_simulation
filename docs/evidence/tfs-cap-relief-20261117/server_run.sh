#!/bin/bash
# 服务器场景运行入口：冒烟/复现/变体。
# 用法：bash server_run.sh <tag> <catalog-name> <mode> [tfs_json]
set -e
export PATH="$HOME/.cargo/bin:$PATH"
export PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS=""
REPO=/root/dv-repo
DV=$REPO/dynamo_vllm_cpu_sim
PY=$DV/.venv/bin/python
BUILD=$HOME/.cache/dynamo-server-sim/build-1.2
RESULTS=$HOME/.cache/dynamo-server-sim/results
TAG="$1"; CATALOG="$2"; MODE="$3"; JSON="$4"
cd "$DV"
OUT="$RESULTS/$TAG"
rm -rf "$OUT"
ARGS=(--server-dynamo "$BUILD/python"
      --catalog "$REPO/benchmark_tools_20261116/catalogs/$CATALOG.json"
      --arrival-pattern original --arrival-scale 1 --wall-time-scale 1
      --mode "$MODE" --alpha 0.1 --output "$OUT")
if [ -n "$JSON" ]; then ARGS+=(--tfs-json "$JSON"); fi
"$PY" scripts/run_server_benchmark.py "${ARGS[@]}" 2>&1 | tail -3
echo "=== comparison ($TAG)"
python3 - "$OUT/comparison.json" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1]).read())
for mode, s in d.items():
    g = s['groups']
    print('%-8s all: n=%d ok=%d mean=%6.0fms p95=%6.0fms | elapsed=%6.2fs read=%.3fGB' % (
        mode, g['all']['requests'], g['all']['succeeded'],
        g['all']['mean_ttft_ms'] or 0, g['all']['p95_ttft_ms'] or 0,
        s['elapsed_model_s'], s['read_bytes'] / 1e9))
    for cls in ('A', 'B', 'C'):
        if cls in g:
            print('         %s: n=%d mean=%6.0fms' % (cls, g[cls]['requests'], g[cls]['mean_ttft_ms'] or 0))
PY
