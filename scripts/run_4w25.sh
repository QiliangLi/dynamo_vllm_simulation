#!/usr/bin/env bash
# Replay one frozen MiniMax trace and one policy on the four-worker CPU stack.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
dv="$repo/dynamo_vllm_cpu_sim"
if [[ $# != 3 ]]; then
    echo "Usage: bash scripts/run_4w25.sh 408|540|mooncake|agentx-narrow|agentx-flex light|tfs|a_cap1 NEW_OUTPUT_DIR" >&2
    exit 2
fi
case "$1" in
    408) bench=synthetic_abchalfb_408_8s_4_25GBps ;;
    540) bench=synthetic_abclonghit_540_12s_4_25GBps ;;
    mooncake) bench=mooncake_head875_360_12s_4_25GBps ;;
    agentx-narrow) bench=agentx_narrow_358_12s_4_25GBps ;;
    agentx-flex) bench=agentx_flex_358_12s_4_25GBps ;;
    *) echo "Unknown workload: $1" >&2; exit 2 ;;
esac
arm=$2
case "$1:$arm" in
    *:light|*:tfs|540:a_cap1|agentx-*:a_cap1) ;;
    *) echo "Unsupported workload/policy: $1/$arm" >&2; exit 2 ;;
esac
python=${DV_PYTHON:-$dv/.venv/bin/python}
server=$(cd "${DYNAMO_PYTHON_DIR:-$HOME/.cache/dynamo-server-sim/build-1.2/python}" && pwd)
upstream=$(cd "${DV_UPSTREAM:-$dv/upstream}" && pwd)
mkdir -p "$(dirname "$3")"
mkdir "$3"
out=$(cd "$3" && pwd)
"$python" "$repo/benchmark/manage.py" export "$bench" --output "$out/input"
harness="$out/harness"
mkdir "$harness"
cp -R "$dv/scripts" "$dv/sim" "$dv/patched" "$harness/"
ln -s "$upstream" "$harness/upstream"
for item in configs minimax_prefill source-fingerprints.json; do ln -s "$dv/$item" "$harness/$item"; done
"$python" "$harness/scripts/apply_ascend_patch.py"
patch -s -d "$harness" -p1 < "$repo/scripts/four_worker/stack.patch"
if [[ $arm != tfs ]]; then patch -s -d "$harness" -p1 < "$repo/scripts/four_worker/$arm.patch"; fi
export PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS='' TMPDIR=/tmp
export DYN_TCP_RPC_HOST=127.0.0.1 DYN_TCP_REQUEST_TIMEOUT=60 DYN_TFS_CONTROL_TIMEOUT_MS=3000
echo "$bench / $arm -> $out/run"
cd "$harness"
"$python" scripts/run_tfs_stack.py --config "$out/input/config.json" --trace "$out/input/trace.jsonl" \
    --workers 4 --mode tfs --no-idle-bypass --server-dynamo "$server" --output "$out/run" > "$out/run.log" 2>&1 || {
    tail -n 15 "$out/run.log" >&2
    exit 1
}
echo "Client: $out/run/report.json"
echo "Checks: $out/run/check.json, $out/run/layer_check.json, $out/run/read_admission.check.json"
"$python" "$repo/scripts/four_worker/summary.py" "$out/run" "$repo/benchmark/$bench" "$arm" | tee "$out/summary.json"
