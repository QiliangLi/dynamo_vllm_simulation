"""Read-only KV pool, SI bytes/s, FIFO per path, dynamic bandwidth sharing.

Disk bandwidth is divided equally among active paths, capped per path, then
proportionally limited by the shared link. This is an explicit policy, not a
max-min-fair or firmware-accurate ASU model. No static size/bandwidth deadlines.
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


class Storage:
    def __init__(self, config, block_size):
        self.c = config
        for k in (
            "disks",
            "paths_per_disk",
            "disk_Bps",
            "shared_link_Bps",
            "path_Bps",
            "kv_bytes_per_token",
        ):
            if config[k] <= 0:
                raise ValueError(f"storage.{k} must be positive")
        self.block_bytes = block_size * config["kv_bytes_per_token"]
        self.present = set()
        self.queues = defaultdict(deque)
        self.pending = {}
        self.now = 0.0
        self.bytes_transferred = 0.0

    def location(self, block):
        disk = max(
            range(self.c["disks"]),
            key=lambda d: hashlib.sha256(block + d.to_bytes(4, "little")).digest(),
        )
        path = (
            int.from_bytes(hashlib.sha256(b"path" + block).digest()[:8], "little")
            % self.c["paths_per_disk"]
        )
        return disk, path

    def submit(self, now, key, blocks):
        assert abs(now - self.now) < 1e-9 and key not in self.pending and blocks
        self.pending[key] = len(blocks)
        for block in blocks:
            assert block in self.present
            self.queues[self.location(block)].append(
                Read(key, self.block_bytes, now + self.c["read_latency_s"])
            )

    def rates(self):
        heads = {
            p: q[0]
            for p, q in self.queues.items()
            if q and q[0].ready <= self.now + 1e-12
        }
        counts = defaultdict(int)
        for d, _ in heads:
            counts[d] += 1
        rates = {
            p: min(self.c["path_Bps"], self.c["disk_Bps"] / counts[p[0]]) for p in heads
        }
        scale = min(1.0, self.c["shared_link_Bps"] / max(1.0, sum(rates.values())))
        return {p: b * scale for p, b in rates.items()}

    def next_time(self):
        times = [
            self.now + self.queues[p][0].remaining / b for p, b in self.rates().items()
        ]
        times += [
            q[0].ready
            for q in self.queues.values()
            if q and q[0].ready > self.now + 1e-12
        ]
        return min(times, default=math.inf)

    def advance(self, target):
        if target < self.now - 1e-12 or target > self.next_time() + 1e-9:
            raise ValueError("must stop at next storage event")
        for p, b in self.rates().items():
            r = self.queues[p][0]
            n = min(r.remaining, b * (target - self.now))
            r.remaining -= n
            self.bytes_transferred += n
        self.now = target
        done = []
        for q in self.queues.values():
            if q and q[0].remaining <= 1e-5:
                r = q.popleft()
                self.pending[r.key] -= 1
                if not self.pending[r.key]:
                    del self.pending[r.key]
                    done.append(r.key)
        return done

    def snapshot(self):
        return {
            "timestamp_s": self.now,
            "queued_reads": sum(len(q) for q in self.queues.values()),
            "active_paths": len(self.rates()),
            "bytes_remaining": sum(
                r.remaining for q in self.queues.values() for r in q
            ),
            "path_rates_Bps": {f"{d}:{p}": b for (d, p), b in self.rates().items()},
        }
