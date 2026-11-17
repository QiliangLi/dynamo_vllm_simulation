"""分析一次运行的 admission.events.jsonl：cap 轨迹、实测样本、等待与拒绝。

用法：python3 analyze_events.py <admission.events.jsonl 路径>
输出 JSON 一行。
"""
import json
import math
import statistics as st
import sys

path = sys.argv[1]
arr = {}
res = {}
caps = []
denied = 0
overdraft = 0
samples = []
basis_counts = {}
for line in open(path, encoding='utf-8'):
    e = json.loads(line)
    ev = e.get('event')
    if ev == 'request_arrived':
        arr[e['id']] = e['t']
    elif ev == 'admission_reserved':
        res[e['id']] = e['t']
        if e.get('reason') == 'overdraft':
            overdraft += 1
    elif ev == 'threshold_updated':
        caps.append((e['t'], e.get('theta'), e.get('cap'), e.get('cap_basis')))
        basis_counts[e.get('cap_basis')] = basis_counts.get(e.get('cap_basis'), 0) + 1
    elif ev == 'read_permit_denied':
        denied += 1
    elif ev == 'heavy_rate_sample':
        samples.append(e['rate_Bps'])

waits = [t - arr[r] for r, t in res.items() if r in arr]
formal_caps = [c for _, _, c, b in caps if c is not None and b != 'no_positive_history' and c < 10 ** 6]
out = dict(
    n_updates=len(caps),
    basis_counts=basis_counts,
    cap_final=caps[-1][2] if caps else None,
    cap_median=st.median(formal_caps) if formal_caps else None,
    cap_min=int(min(formal_caps)) if formal_caps else None,
    cap_max_bounded=int(max(formal_caps)) if formal_caps else None,
    theta_final=caps[-1][1] if caps else None,
    measured_samples=len(samples),
    measured_median_Bps=st.median(samples) if samples else None,
    grants=len(res),
    denied=denied,
    overdraft=overdraft,
    wait_median_ms=st.median(waits) * 1000 if waits else None,
    wait_p90_ms=sorted(waits)[math.ceil(len(waits) * .9) - 1] * 1000 if waits else None,
    wait_max_ms=max(waits) * 1000 if waits else None,
)
print(json.dumps(out, ensure_ascii=False))
