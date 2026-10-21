#!/usr/bin/env bash
# 一键进程栈实验：起栈 → 就绪 → 回放 → 验收 → 自动停栈（W6 运维闭环）。
#
# 用法（在 dynamo_vllm_cpu_sim/ 下）：
#   bash scripts/run_stack_experiment.sh --name stack_remote                 # 远端 KV 命中
#   bash scripts/run_stack_experiment.sh --name stack_smoke --no-remote-kv --trace configs/stack_smoke.jsonl
#   bash scripts/run_stack_experiment.sh --name stack_slow --sim-config configs/stack_slow.json
#
# 保证：无论成功、失败还是 Ctrl-C，栈进程都会被停掉（trap + 跨运行登记表兜底）；
# 每次实验用独立的发现目录与日志目录（LOG_DIR/FILE_KV 隔离，避免跨实验互相污染）。
# 产物：results/<name>/report.json；日志与逐步调度记录在 /tmp/stack_logs/<name>/。
# 注意：并发跑多个实验需自行错开 STACK_HTTP_PORT/STACK_BASE_SYS_PORT（见环境变量）。

set -euo pipefail
cd "$(dirname "$0")/.."

NAME="stack_remote"
TRACE="configs/stack_remote.jsonl"
SIM_CONFIG=""
WORKERS=2
REMOTE_KV=1
API="completions"
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --trace) TRACE="$2"; shift 2 ;;
    --sim-config) SIM_CONFIG="$2"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --no-remote-kv) REMOTE_KV=0; shift ;;
    --api) API="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done

LOG_DIR="/tmp/stack_logs/$NAME"
FILE_KV="/tmp/dyn_kv_$NAME"
RESULTS="results/$NAME"
export STACK_LOG_DIR="$LOG_DIR" STACK_FILE_KV="$FILE_KV"
[ -n "$SIM_CONFIG" ] && export STACK_SIM_CONFIG="$PWD/$SIM_CONFIG"
export STACK_REMOTE_KV="$REMOTE_KV" STACK_TRACE="$TRACE"

stop_stack() {
  bash scripts/launch_stack.sh stop >/dev/null 2>&1 || true
}
trap stop_stack EXIT INT TERM

echo "==> 起栈: workers=${WORKERS} remote_kv=${REMOTE_KV} trace=${TRACE} sim_config=${SIM_CONFIG:-demo}"
bash scripts/launch_stack.sh up "$WORKERS" | sed 's/^/    /'

echo "==> 等待 frontend 就绪"
READY=0
for _ in $(seq 1 60); do
  if curl -s --max-time 2 "http://127.0.0.1:${STACK_HTTP_PORT:-8765}/v1/models" 2>/dev/null | grep -q sim-opt; then
    READY=1; break
  fi
  sleep 3
done
[ "$READY" = "1" ] || { echo "frontend 未就绪，看 $LOG_DIR/frontend.log" >&2; exit 1; }

echo "==> 回放 $TRACE -> $RESULTS"
.venv/bin/python scripts/replay_http.py "$TRACE" --model sim-opt \
  --api "$API" --output "$RESULTS"

echo "==> 验收断言"
.venv/bin/python scripts/check_stack.py "$RESULTS"

echo "==> 停栈（trap 兜底，此处显式执行）"
stop_stack
trap - EXIT INT TERM
echo "完成：$RESULTS/report.json；日志 $LOG_DIR"
