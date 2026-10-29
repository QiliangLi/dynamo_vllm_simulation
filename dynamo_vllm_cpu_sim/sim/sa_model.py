"""SA E26f 的画像与聚合层读取；不依赖 Dynamo/vLLM，不读取控制器状态。"""

from dataclasses import dataclass
from fractions import Fraction
import math

from sim.storage import waterfill


def layer_compute(shapes, profile):
    """与 SA batch_compute_s 同式：真实本步 token、有效 attention、批级 eta。"""
    n = sum(u for h, u in shapes)
    pairs = sum(u*h + u*(u+1)//2 for h, u in shapes)
    eta = min(1.0, max(float(Fraction(profile["eta_floor"])), n/profile["N_sat"]))
    return (float(Fraction(profile["t_launch_s"]))
            + float(Fraction(profile["a_s_per_token"]))*n/eta
            + float(Fraction(profile["b_s_per_pair"]))*pairs)


def reference(row, profile, bandwidth, qmax):
    """SA singleton_K 解析值；不使用当前运行结果生成期限。"""
    v = float(Fraction(profile["kappa_gb_per_token_layer"]))*1e9*row["h_tokens"]
    c = layer_compute([(row["h_tokens"], row["u_tokens"])], profile)
    read = v/min(bandwidth, qmax)
    layers = profile["L"]
    return v, c, read + layers*c + (layers-1)*max(0.0, read-c)


@dataclass
class LayerFlow:
    key: tuple
    remaining: float
    q: float
    due: float | None
    rate: float = 0.0
    boosted: bool = False


class LayerStorage:
    """与 SA CQ 相同的聚合 max_min 分配；每个 batch/layer 一个流，无盘/路径。"""

    def __init__(self, bandwidth, qmax, alloc="max_min"):
        if min(bandwidth, qmax) <= 0 or alloc not in ("max_min", "fcfs"):
            raise ValueError("invalid SA storage")
        self.bandwidth, self.qmax, self.alloc = bandwidth, qmax, alloc
        self.now = 0.0
        self.flows = {}
        self.bytes_transferred = self.bytes_submitted = 0.0
        self._byte_compensation = 0.0
        self.peak_flows = 0
        self.events = []

    def _add_bytes(self, value):
        """补偿求和避免 200GB 级、数万个小区间的累计舍入漂移。"""
        corrected = value-self._byte_compensation
        total = self.bytes_transferred+corrected
        self._byte_compensation = (total-self.bytes_transferred)-corrected
        self.bytes_transferred = total

    def submit(self, key, size, q, due=None):
        if key in self.flows or min(size, q) <= 0:
            raise ValueError("duplicate/nonpositive layer flow")
        self.flows[key] = LayerFlow(key, size, q, due)
        self.bytes_submitted += size
        self.peak_flows = max(self.peak_flows, len(self.flows))
        self.events.append({"t": self.now, "event": "layer_read_submitted",
                            "key": list(key), "bytes": size, "q_Bps": q, "due_s": due})

    def allocate(self):
        flows = list(self.flows.values())
        if self.alloc == "max_min":
            rates = waterfill(self.bandwidth, [f.q for f in flows]) if flows else []
        else:
            free, rates = self.bandwidth, []
            for f in flows:
                r = min(f.q, max(0.0, free))
                rates.append(r)
                free -= r
        for f, rate in zip(flows, rates):
            f.rate = rate

    def next_time(self):
        times = [self.now + f.remaining/f.rate for f in self.flows.values() if f.rate > 0]
        times += [f.due for f in self.flows.values()
                  if not f.boosted and f.due is not None and f.due > self.now]
        return min(times, default=math.inf)

    def advance(self, target):
        if target < self.now or target > self.next_time()+1e-10:
            raise ValueError("must stop at earliest storage event")
        if self.flows:
            self.events.append({"t": target, "event": "storage_interval", "from": self.now,
                                "actual_Bps": sum(f.rate for f in self.flows.values()),
                                "demand_Bps": sum(f.q for f in self.flows.values()),
                                "active_flows": len(self.flows)})
        dt = target-self.now
        sent_values = []
        for f in self.flows.values():
            sent = min(f.remaining, f.rate*dt)
            f.remaining -= sent
            sent_values.append(sent)
        self._add_bytes(math.fsum(sent_values))
        self.now = target
        done = []
        for key, f in list(self.flows.items()):
            # 与 SA 1e-12 GB 容差同量级；不得丢弃整块/整层字节。
            if f.remaining <= 1e-3:
                self._add_bytes(f.remaining)
                done.append(key)
                del self.flows[key]
                self.events.append({"t": target, "event": "layer_read_complete", "key": list(key)})
            elif not f.boosted and f.due is not None and f.due <= target:
                f.q, f.boosted = self.qmax, True
                self.events.append({"t": target, "event": "layer_read_boosted", "key": list(key),
                                    "q_Bps": f.q})
        return done
