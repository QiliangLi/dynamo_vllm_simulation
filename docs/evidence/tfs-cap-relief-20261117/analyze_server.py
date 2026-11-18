"""分析服务器场景 tfs 运行的 admission 事件：cap 轨迹、等待分布、超发计数。"""
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
basis = {}
waits_by_class = {}
for line in open(path, encoding='utf-8'):
    e = json.loads(line)
    ev = e.get('event')
    if ev == 'request_arrived':
        arr[e['id']] = e['t']
    elif ev == 'admission_reserved':
        res[e['id']] = e['t']
        if e.get('reason') == 'overdraft':
            overdraft += 1
        cls = e.get('profile', {}).get('class_id', '?')
        waits_by_class.setdefault(cls, []).append(e['t'] - arr.get(e['id'], e['t']))
    elif ev == 'threshold_updated':
        caps.append((e['t'], e.get('theta'), e.get('cap')))
        basis[e.get('cap_basis')] = basis.get(e.get('cap_basis'), 0) + 1
    elif ev == 'read_permit_denied':
        denied += 1
    elif ev == 'heavy_rate_sample':
        samples.append(e['rate_Bps'])

waits = [t - arr[r] for r, t in res.items() if r in arr]
formal = [c for _, _, c in caps if c is not None and c < 10 ** 6]
print(json.dumps(dict(
    n_updates=len(caps), basis=basis,
    cap_first_bounded=formal[0] if formal else None,
    cap_median=st.median(formal) if formal else None,
    cap_final=caps[-1][2] if caps else None,
    theta_final_gbps=round((caps[-1][1] or 0) / 1e9, 2) if caps else None,
    grants=len(res), denied=denied, overdraft=overdraft,
    measured_samples=len(samples),
    measured_median_gbps=round(st.median(samples) / 1e9, 2) if samples else None,
    wait_median_ms=round(st.median(waits) * 1000, 1) if waits else None,
    wait_p90_ms=round(sorted(waits)[math.ceil(len(waits) * .9) - 1] * 1000, 1) if waits else None,
    wait_max_ms=round(max(waits) * 1000, 1) if waits else None,
    wait_by_class_ms={k: round(st.median(v) * 1000, 1) for k, v in sorted(waits_by_class.items())},
), ensure_ascii=False))
