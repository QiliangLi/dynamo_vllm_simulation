"""Read-only KV pool, SI bytes/s, two bandwidth policies, dynamic sharing.

Blocks are placed on a disk by content hash and are visible to every path of
that disk; an IO request is dispatched to a path only when it is submitted
(least-occupied path, ties to the lowest index).

storage.bandwidth_policy selects the rate rule:
- "active_split" (default): per-disk bandwidth split equally among active
  path heads, capped per path. Paths are FIFO queues; with more concurrent
  IO requests than paths, requests queue on the least-occupied path.
- "max_min_fair": demand-capped water-filling per ASU. Every IO request
  exclusively occupies its own path (paths are unbounded, no queueing) and
  carries a demand rate. When demands fit the disk, each gets its demand
  plus an equal share of the surplus; otherwise allocations are max-min
  fair: equal split, cap over-demanded requests at their demand, iterate.
An optional shared_link_Bps further limits rates. None disables this global
constraint: disks then supply bandwidth independently, with no borrowing.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import math


@dataclass
class Read:
    key: tuple[int, str]
    remaining: float
    ready: float
    demand: float = None


def waterfill(capacity, demands):
    """Max-min fair allocation with demand caps, iterated to a fixed point."""
    n = len(demands)
    total = sum(demands)
    if total <= capacity:
        bonus = (capacity - total) / n
        return [d + bonus for d in demands]
    alloc = [0.0] * n
    cap = capacity
    for i in sorted(range(n), key=lambda i: demands[i]):
        share = cap / n
        alloc[i] = min(demands[i], share)
        cap -= alloc[i]
        n -= 1
    return alloc


class Storage:
    def __init__(self, config, block_size):
        self.c = config
        for k in (
            "disks",
            "paths_per_disk",
            "disk_Bps",
            "path_Bps",
            "kv_bytes_per_token",
        ):
            if not math.isfinite(config[k]) or config[k] <= 0:
                raise ValueError(f"storage.{k} must be positive")
        link = config.get("shared_link_Bps")
        if link is not None and (not math.isfinite(link) or link <= 0):
            raise ValueError("storage.shared_link_Bps must be positive or null")
        self.link_Bps = link
        self.policy = config.get("bandwidth_policy", "active_split")
        if self.policy not in ("active_split", "max_min_fair"):
            raise ValueError(
                f"storage.bandwidth_policy must be active_split or max_min_fair,"
                f" got {self.policy!r}"
            )
        self.block_bytes = block_size * config["kv_bytes_per_token"]
        self.present = set()
        self.queues = defaultdict(deque)
        self.path_seq = defaultdict(int)
        self.pending = {}
        self.now = 0.0
        self.bytes_transferred = 0.0
        self.disk_bytes_submitted = [0] * config["disks"]
        self.disk_bytes_transferred = [0.0] * config["disks"]
        self.disk_peak_Bps = [0.0] * config["disks"]

    def disk_of(self, block):
        """HRW placement using the real vLLM content hash, independent of worker.

        Keep DV's existing binary key encoding to preserve existing placement;
        SA uses the same argmax-SHA256 algorithm with a different key encoding.
        """
        if self.c["disks"] == 1:
            return 0
        return max(
            range(self.c["disks"]),
            key=lambda d: hashlib.sha256(block + d.to_bytes(4, "little")).digest(),
        )

    def _pick_path(self, disk):
        lengths = {
            p: len(q) for (d, p), q in self.queues.items() if d == disk and q
        }
        if len(lengths) < self.c["paths_per_disk"]:
            for p in range(self.c["paths_per_disk"]):
                if p not in lengths:
                    return p
        return min(lengths, key=lambda p: (lengths[p], p))

    def submit(self, now, key, blocks, demand=None):
        assert abs(now - self.now) < 1e-9 and key not in self.pending and blocks
        assert all(block in self.present for block in blocks)
        if self.policy == "max_min_fair":
            assert demand and demand > 0, "max_min_fair requires a positive demand"
        self.pending[key] = len(blocks)
        for block in blocks:
            assert block in self.present
            disk = self.disk_of(block)
            self.disk_bytes_submitted[disk] += self.block_bytes
            if self.policy == "max_min_fair":
                path = self.path_seq[disk]
                self.path_seq[disk] += 1
            else:
                path = self._pick_path(disk)
            self.queues[(disk, path)].append(
                Read(key, self.block_bytes, now + self.c["read_latency_s"], demand)
            )

    def _raw_rates(self):
        """Per-path rates before the shared link is applied."""
        heads = {
            p: q[0]
            for p, q in self.queues.items()
            if q and q[0].ready <= self.now + 1e-12
        }
        if self.policy == "max_min_fair":
            by_disk = defaultdict(list)
            for p, r in heads.items():
                by_disk[p[0]].append((p, r.demand))
            raw = {}
            for disk, items in by_disk.items():
                allocs = waterfill(self.c["disk_Bps"], [d for _, d in items])
                raw.update(zip((p for p, _ in items), allocs))
            return raw
        counts = defaultdict(int)
        for d, _ in heads:
            counts[d] += 1
        return {
            p: min(self.c["path_Bps"], self.c["disk_Bps"] / counts[p[0]]) for p in heads
        }

    def rates(self):
        raw = self._raw_rates()
        scale = self._link_scale(sum(raw.values()))
        return {p: b * scale for p, b in raw.items()}

    def _link_scale(self, total):
        return (1.0 if self.link_Bps is None else
                min(1.0, self.link_Bps / max(1.0, total)))

    def link_flux(self):
        """(demand_Bps, actual_Bps)：施加共享链路缩放前/后的活跃读速率总和。

        demand 是"若链路无限"时盘/路径层级决定的速率和，actual 是链路后的
        实际速率；二者之差即链路瓶颈压掉的带宽。供 opt-in 区间日志与带宽
        时序图消费（run.py 的 storage_interval_log）。
        """
        demand = sum(self._raw_rates().values())
        scale = self._link_scale(demand)
        return demand, demand * scale

    def next_time(self):
        return self._next_time_for_rates(self.rates())

    def _next_time_for_rates(self, rates):
        """Find the event boundary using rates from the unchanged current state."""
        times = [
            self.now + self.queues[p][0].remaining / b for p, b in rates.items()
        ]
        times += [
            q[0].ready
            for q in self.queues.values()
            if q and q[0].ready > self.now + 1e-12
        ]
        return min(times, default=math.inf)

    def advance(self, target):
        rates = self.rates()
        if target < self.now - 1e-12 or target > self._next_time_for_rates(rates) + 1e-9:
            raise ValueError("must stop at next storage event")
        by_disk = defaultdict(float)
        for (d, _), rate in rates.items():
            by_disk[d] += rate
        for d, rate in by_disk.items():
            self.disk_peak_Bps[d] = max(self.disk_peak_Bps[d], rate)
        for p, b in rates.items():
            r = self.queues[p][0]
            n = min(r.remaining, b * (target - self.now))
            r.remaining -= n
            self.bytes_transferred += n
            self.disk_bytes_transferred[p[0]] += n
        self.now = target
        done = []
        empty_paths = []
        for path, q in self.queues.items():
            if q and q[0].remaining <= 1e-5:
                r = q.popleft()
                self.pending[r.key] -= 1
                if not self.pending[r.key]:
                    del self.pending[r.key]
                    done.append(r.key)
                if not q and self.policy == "max_min_fair":
                    empty_paths.append(path)
        # Max-min paths have unique, never reused IDs. Removing retired paths
        # preserves the order of all remaining reads and simultaneous completions.
        # active_split reuses IDs: keep its bounded empty queues so reinsertion
        # cannot change the original completion tie order.
        for path in empty_paths:
            del self.queues[path]
        return done

    def snapshot(self):
        rates = self.rates()
        disk_rates = [0.0] * self.c["disks"]
        for (d, _), rate in rates.items():
            disk_rates[d] += rate
        return {
            "placement": "hrw_sha256_content_hash",
            "disks": self.c["disks"],
            "disk_Bps": self.c["disk_Bps"],
            "shared_link_Bps": self.link_Bps,
            "disk_rates_Bps": disk_rates,
            "disk_peak_Bps": self.disk_peak_Bps.copy(),
            "disk_bytes_submitted": self.disk_bytes_submitted.copy(),
            "disk_bytes_transferred": self.disk_bytes_transferred.copy(),
            "timestamp_s": self.now,
            "queued_reads": sum(len(q) for q in self.queues.values()),
            "active_paths": len(rates),
            "bytes_remaining": sum(
                r.remaining for q in self.queues.values() for r in q
            ),
            "path_rates_Bps": {f"{d}:{p}": b for (d, p), b in rates.items()},
        }
