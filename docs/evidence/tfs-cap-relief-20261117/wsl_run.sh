#!/bin/bash
# 探索轮执行/分析统一入口（在 WSL 内运行）。
# 用法：bash wsl_run.sh <command>
#   analyze <run_dir>            分析某 run 目录下 tfs-a01 的 admission 事件
#   smoke <tag> <tfs_json>       冒烟
#   formal <tag> <case> <arm> <tfs_json>  正式探索轮（新输出目录 e-<tag>）
set -e
REPO=/root/dv-repo/dynamo_vllm_cpu_sim
P=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117
PY=$REPO/.venv/bin/python
OUT=$REPO/results/benchmark_runs

cmd="$1"
case "$cmd" in
  analyze)
    echo "== $2"
    find "$OUT/$2" -name admission.events.jsonl | while read -r f; do
      echo "-- ${f#$OUT/}"
      python3 "$P/analyze_events.py" "$f"
    done
    python3 "$P/collect_results.py" "$OUT/$2"
    ;;
  smoke)
    tag="$2"; json="$3"
    rm -rf "$OUT/e0-smoke-$tag"
    BENCH_TFS_JSON="$json" "$PY" "$REPO/scripts/run_benchmark.py" \
      --case mooncake_abc_360_4s_12_90GBps --arm tfs-a01 --smoke \
      --dynamo-python "$REPO/build/weighted-dynamo-8/python" \
      --output "$OUT/e0-smoke-$tag" 2>&1 | tail -2
    ;;
  formal)
    tag="$2"; case_name="$3"; arm="$4"; json="$5"; shift 5
    rm -rf "$OUT/e-$tag"
    if [ "$json" = "-" ]; then unset BENCH_TFS_JSON; else export BENCH_TFS_JSON="$json"; fi
    "$PY" "$REPO/scripts/run_benchmark.py" \
      --case "$case_name" --arm "$arm" "$@" \
      --dynamo-python "$REPO/build/weighted-dynamo-8/python" \
      --output "$OUT/e-$tag" 2>&1 | tail -2
    unset BENCH_TFS_JSON
    ;;
  *)
    echo "unknown command: $cmd" >&2; exit 1 ;;
esac
