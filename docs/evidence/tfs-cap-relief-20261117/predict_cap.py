"""静态预判 synthetic_abc 的 θ 与 cap（不启动完整栈）。

用 minimax_server_3w.json 的计算/存储配置与 catalog H/U 构造画像，
按 sim/reference_time.py 口径计算 pressure_Bps，再算 P75 分位与
floor(reference_bandwidth/θ)。正式窗口历史含 C 类零压力请求。
"""
import json
import math
import sys
from pathlib import Path

DV = Path('/root/dv-repo/dynamo_vllm_cpu_sim')
sys.path.insert(0, str(DV))

from sim.request_profile import normalize_config  # noqa: E402

cfg = json.loads((DV / 'configs/minimax_server_3w.json').read_text())
cfg = normalize_config(cfg)
catalog = json.loads(Path(sys.argv[1]).read_text()
                     if len(sys.argv) > 1 else
                     (DV.parent / 'benchmark_tools_20261116/catalogs/synthetic_abc.json').read_text())

from sim.reference_time import get_reference_time_estimator  # noqa: E402
est = get_reference_time_estimator(cfg)
pressures = []
per_class = {}
for r in catalog['requests']:
    n, h = r['miss_tokens'] + r['hit_tokens'], r['hit_tokens']
    ref = est.estimate(n, h)  # h=0 合法：pressure=0 计入全体历史（C 类）
    pressures.append(ref.pressure_Bps)
    per_class.setdefault(r['request_class'], []).append(
        (h, n, ref.compute_s, ref.pressure_Bps, ref.read_bytes))
pressures.sort()
q = cfg['admission']['quantile']
theta = pressures[math.floor(q * (len(pressures) - 1))]
bw = cfg['admission']['reference_bandwidth_Bps']
cap = max(1, math.floor(bw / theta)) if theta else None
print('reference_bandwidth_Bps =', bw)
for cls, rows in sorted(per_class.items()):
    h, n, c, p, b = rows[0]
    print(f'class {cls}: H={h} n={n} compute_s={c:.4f} pressure={p/1e9:.2f}GB/s read={b/1e9:.3f}GB x{len(rows)}')
print(f'theta(P{q*100:.0f}) = {theta/1e9:.3f} GB/s -> cap = {cap}')
