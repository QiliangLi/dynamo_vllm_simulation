"""按目录汇总 benchmark.check.json 关键指标；供 WSL 侧执行。

用法：python3 collect_results.py /root/dv-repo/dynamo_vllm_cpu_sim/results/benchmark_runs [名称过滤]
"""
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
filt = sys.argv[2] if len(sys.argv) > 2 else ''
for d in sorted(base.iterdir()):
    if not d.is_dir() or filt not in d.name:
        continue
    for arm_dir in sorted(d.rglob('run/benchmark.check.json')):
        r = json.loads(arm_dir.read_text())
        print('%-34s %-10s mean=%6.0f p95=%6.0f slo=%5.1f done=%7.0f %s' % (
            d.name[:34], r.get('arm', '?'), r['mean_ttft_ms'],
            r['p95_ttft_ms'], r['slo_pct'], r['completion_ms'],
            'SMOKE' if r.get('smoke') else ''))
