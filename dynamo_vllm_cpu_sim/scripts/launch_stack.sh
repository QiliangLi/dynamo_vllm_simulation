#!/usr/bin/env bash
# 进程化仿真栈一键起停（W4，设计文档 §5.4）。
#
# 用法（在 dynamo_vllm_cpu_sim/ 下）：
#   bash scripts/launch_stack.sh up [N_WORKERS]     # 默认 2；起 frontend + N×dynamo.vllm worker
#   bash scripts/launch_stack.sh stop               # 停全部进程并保留日志
#
# 一键"起栈→回放→验收→自动停栈"请用 scripts/run_stack_experiment.sh（推荐），
# 它保证实验结束（含失败/中断）后栈一定被停掉。
#
# 环境变量（可覆盖）：
#   STACK_HTTP_PORT（默认 8765）、STACK_MODEL_DIR（默认 configs/model）、
#   STACK_MODEL_NAME（默认 sim-opt）、STACK_MAX_MODEL_LEN（默认 2048）、
#   STACK_NUM_BLOCKS（默认 128，须满足 num_blocks*16 ≥ max_model_len）、
#   STACK_LOG_DIR（默认 /tmp/stack_logs/stack）、STACK_FILE_KV（默认 /tmp/dyn_stack_kv）、
#   STACK_REGISTRY（默认 /tmp/stack_logs/.registry，跨运行登记所有已起进程）
#
# 说明：file 发现后端 + tcp 请求面 + ZMQ 事件面（免 etcd/NATS）；每个 worker
# 需要独立的 DYN_SYSTEM_PORT（元数据自托管）；worker 经 scripts/stack_vllm_worker.py
# 包装启动（SimExecutor 限定名注入 + 0.20.2↔0.28 兼容层），vLLM/Dynamo 源码零修改。
# 资源占用（2026-10-21 实测，2 worker）：空闲全栈 ~0 CPU、~0.3–0.5GB RSS；
# mock 执行为 time.sleep，无 GPU/NPU 占用；用完 stop 即可。

set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
HTTP_PORT="${STACK_HTTP_PORT:-8765}"
MODEL_DIR="${STACK_MODEL_DIR:-configs/model}"
MODEL_NAME="${STACK_MODEL_NAME:-sim-opt}"
MAX_MODEL_LEN="${STACK_MAX_MODEL_LEN:-2048}"
NUM_BLOCKS="${STACK_NUM_BLOCKS:-128}"
LOG_DIR="${STACK_LOG_DIR:-/tmp/stack_logs/stack}"
FILE_KV="${STACK_FILE_KV:-/tmp/dyn_stack_kv}"
BASE_SYS_PORT="${STACK_BASE_SYS_PORT:-9111}"
REGISTRY="${STACK_REGISTRY:-/tmp/stack_logs/.registry}"
# 远端 KV 模式（W5）：=1 时先起 storage-sim，worker 挂 SimConnectorRpc。
# STACK_TRACE 同时用于共享池预置（与 DES run.py 相同的 remote_prefix_tokens 语义）。
REMOTE_KV="${STACK_REMOTE_KV:-0}"
TRACE="${STACK_TRACE:-configs/stack_remote.jsonl}"

COMMON_ENV=(
  DYN_DISCOVERY_BACKEND=file "DYN_FILE_KV=$FILE_KV" DYN_REQUEST_PLANE=tcp
  VLLM_PLUGINS='' HF_HUB_OFFLINE=1 PYTHONHASHSEED=0 "PYTHONPATH=$PWD"
)
# 注意：vLLM 前缀缓存块哈希依赖 Python 内建 hash()（受 PYTHONHASHSEED 影响），
# 跨进程共享池（storage-sim 预置、worker 命中查询）必须统一 seed；DES 单进程
# 固定 0，这里同样固定 0。

KV_FLAGS=()
if [ "$REMOTE_KV" = "1" ]; then
  KV_FLAGS=(--kv-transfer-config '{"kv_connector":"SimConnector","kv_role":"kv_both"}')
fi

register_pid() {  # register_pid <pid> <marker>：跨运行登记，stop 时兜底回收
  mkdir -p "$(dirname "$REGISTRY")"
  echo "$1 $2" >> "$REGISTRY"
}

pid_alive_matches() {  # pid_alive_matches <pid> <marker>：防 PID 复用误杀
  local cmd
  cmd=$(ps -p "$1" -o command= 2>/dev/null) || return 1
  [[ "$cmd" == *"$2"* ]]
}

case "${1:-}" in
up)
  N="${2:-2}"
  mkdir -p "$LOG_DIR" "$FILE_KV"
  if [ "$REMOTE_KV" = "1" ]; then
    env "${COMMON_ENV[@]}" "SIM_STACK_CONFIG=${STACK_SIM_CONFIG:-$PWD/configs/demo.json}" \
      nohup "$PY" -m sim.stack.storage_service \
      --config "${STACK_SIM_CONFIG:-$PWD/configs/demo.json}" --trace "$TRACE" \
      --socket "$LOG_DIR/storage.sock" \
      > "$LOG_DIR/storage.log" 2>&1 &
    echo $! > "$LOG_DIR/storage.pid"
    register_pid "$(cat "$LOG_DIR/storage.pid")" "sim.stack.storage_service"
    for _ in $(seq 1 20); do [ -S "$LOG_DIR/storage.sock" ] && break; sleep 0.5; done
    echo "storage-sim pid $(cat "$LOG_DIR/storage.pid") ($LOG_DIR/storage.sock, pool from $TRACE)"
  fi
  env "${COMMON_ENV[@]}" nohup "$PY" -m dynamo.frontend \
    --http-port "$HTTP_PORT" --router-mode round-robin \
    > "$LOG_DIR/frontend.log" 2>&1 &
  echo $! > "$LOG_DIR/frontend.pid"
  register_pid "$(cat "$LOG_DIR/frontend.pid")" "dynamo.frontend"
  echo "frontend pid $(cat "$LOG_DIR/frontend.pid") (http :$HTTP_PORT)"
  sleep 3
  for W in $(seq 0 $((N - 1))); do
    WORKER_ENV=("${COMMON_ENV[@]}" "SIM_STACK_WORKER_ID=$W" "SIM_STACK_TOKEN_ID=290" \
      "DYN_SYSTEM_PORT=$((BASE_SYS_PORT + W))" \
      "SIM_STACK_CONFIG=${STACK_SIM_CONFIG:-$PWD/configs/demo.json}" \
      "SIM_STACK_STEP_LOG=$LOG_DIR/worker$W.steps.jsonl")
    if [ "$REMOTE_KV" = "1" ]; then
      WORKER_ENV+=("SIM_STACK_STORAGE_SOCKET=$LOG_DIR/storage.sock")
    fi
    # 注意 ${KV_FLAGS[@]+"${KV_FLAGS[@]}"}：macOS bash 3.2 在 set -u 下展开空数组
    # 会报 unbound variable，此写法对空/非空都安全。
    env "${WORKER_ENV[@]}" nohup "$PY" scripts/stack_vllm_worker.py \
      --model "$MODEL_DIR" --served-model-name "$MODEL_NAME" \
      --endpoint dyn://dynamo.backend.generate \
      --dtype float32 --max-model-len "$MAX_MODEL_LEN" --block-size 16 \
      --num-gpu-blocks-override "$NUM_BLOCKS" --max-num-seqs 8 \
      --max-num-batched-tokens 128 --enforce-eager --seed 0 \
      ${KV_FLAGS[@]+"${KV_FLAGS[@]}"} \
      > "$LOG_DIR/worker$W.log" 2>&1 &
    echo $! > "$LOG_DIR/worker$W.pid"
    register_pid "$(cat "$LOG_DIR/worker$W.pid")" "stack_vllm_worker"
    echo "worker$W pid $(cat "$LOG_DIR/worker$W.pid") (metadata :$((BASE_SYS_PORT + W)))"
    sleep 5
  done
  echo "logs: $LOG_DIR   (bash scripts/launch_stack.sh stop 可停止)"
  ;;
stop)
  # 1) 当前 LOG_DIR 的 pid 文件
  for P in "$LOG_DIR"/*.pid; do
    [ -e "$P" ] || continue
    PID=$(cat "$P")
    if kill "$PID" 2>/dev/null; then echo "stopped $(basename "$P" .pid) ($PID)"; fi
    rm -f "$P"
  done
  # 2) 跨运行登记表兜底：回收任何 LOG_DIR/命名不同的历史残留（先校验命令行防 PID 复用）
  if [ -f "$REGISTRY" ]; then
    while read -r PID MARKER; do
      [ -n "${PID:-}" ] || continue
      if pid_alive_matches "$PID" "$MARKER"; then
        kill "$PID" 2>/dev/null && echo "reaped stray $MARKER ($PID)"
      fi
    done < "$REGISTRY"
    rm -f "$REGISTRY"
  fi
  # EngineCore 子进程由 worker 主进程退出时自行回收；兜底清理并等待其退出
  sleep 1
  pkill -f "VLLM::EngineCore" 2>/dev/null || true
  for _ in $(seq 1 20); do
    LEFT=$(ps aux | grep -E 'dynamo\.frontend|stack_vllm_worker|VLLM::EngineCore|sim\.stack\.storage_service' | grep -v grep | wc -l | tr -d ' ')
    [ "$LEFT" = "0" ] && break
    sleep 0.5
  done
  ;;
*)
  echo "usage: bash scripts/launch_stack.sh up [N_WORKERS] | stop" >&2
  exit 2
  ;;
esac
