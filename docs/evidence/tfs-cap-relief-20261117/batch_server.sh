#!/bin/bash
# E-S2 变体矩阵：overdraft / fixed3 / measured / dev。
set -e
W=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/server_run.sh
A=/mnt/d/Coding/dynamo_vllm_simulation/docs/evidence/tfs-cap-relief-20261117/analyze_server.py
R=/root/.cache/dynamo-server-sim/results
echo "=== od(1s,+2) $(date +%T)"
bash "$W" s2-od synthetic_abc tfs '{"cap_policy":"legacy","overdraft_wait_s":1.0,"overdraft_max_extra":2}'
python3 "$A" "$R/s2-od/tfs/admission.events.jsonl"
echo "=== fixed3 $(date +%T)"
bash "$W" s2-fx3 synthetic_abc tfs '{"cap_policy":"fixed","cap_fixed_int":3}'
python3 "$A" "$R/s2-fx3/tfs/admission.events.jsonl"
echo "=== measured $(date +%T)"
bash "$W" s2-measured synthetic_abc tfs '{"cap_policy":"measured"}'
python3 "$A" "$R/s2-measured/tfs/admission.events.jsonl"
echo "=== dev $(date +%T)"
export PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS=""
cd /root/dv-repo/dynamo_vllm_cpu_sim
rm -rf "$R/s2-dev"
.venv/bin/python scripts/run_server_benchmark.py \
  --server-dynamo "$HOME/.cache/dynamo-server-sim/build-1.2/python" \
  --catalog /root/dv-repo/benchmark_tools_20261116/catalogs/synthetic_abc.json \
  --arrival-pattern original --arrival-scale 1 --wall-time-scale 1 \
  --mode tfs --alpha 0.1 --dev --output "$R/s2-dev" 2>&1 | tail -1
python3 - "$R/s2-dev/comparison.json" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1]).read())
for mode, s in d.items():
    g = s['groups']
    print('%-8s all: mean=%6.0fms p95=%6.0fms | elapsed=%6.2fs' % (
        mode, g['all']['mean_ttft_ms'] or 0, g['all']['p95_ttft_ms'] or 0, s['elapsed_model_s']))
    print('   ', ' '.join('%s=%0.fms' % (c, g[c]['mean_ttft_ms'] or 0) for c in 'ABC' if c in g))
PY
python3 "$A" "$R/s2-dev/tfs/admission.events.jsonl"
echo "ALL_DONE $(date +%T)"
