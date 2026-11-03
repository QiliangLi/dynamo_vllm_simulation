"""Weighted representation of the existing per-block max-min storage model.

Only blocks in the same submission and on the same disk are grouped. They
have identical ready times, remaining bytes and per-block demand, so their
per-block rates remain equal through every later arrival and completion.
The DES Storage and active_split FIFO implementation remain independent.
"""
from collections import defaultdict
from dataclasses import dataclass
import math

from sim.storage import Read, Storage


@dataclass
class ReadGroup(Read):
    # remaining and demand are PER BLOCK, not totals for the group.
    block_count: int = 1
    # Layer reads are smaller and more numerous. The legacy per-block tail
    # threshold can discard several aggregate bytes across 78 submissions.
    # Counted layer reads must consume their entire remaining amount.
    complete_exactly: bool = False


def weighted_waterfill(capacity, demands, weights):
    """Return PER BLOCK rates, equivalent to expanding each demand weight times.

    Preserve both branches of waterfill: demand plus equal per-block surplus
    when demands fit; otherwise progressive max-min filling with demand caps.
    No request-level fairness or rounding of block counts is introduced.
    """
    count = sum(weights)
    total = sum(d * w for d, w in zip(demands, weights))
    if total <= capacity:
        bonus = (capacity - total) / count
        return [d + bonus for d in demands]
    alloc = [0.] * len(demands)
    remaining = capacity
    for i in sorted(range(len(demands)), key=lambda i: demands[i]):
        alloc[i] = min(demands[i], remaining / count)
        remaining -= alloc[i] * weights[i]
        count -= weights[i]
    return alloc


class GroupedStorage(Storage):
    """Max-min storage with one physical read object per request/disk.

    queues contains groups; pending and snapshot counts remain logical block
    counts. rates() and snapshot path rates expand on demand for compatibility.
    Internal time advancement never expands groups into individual paths.
    """
    def __init__(self, config, block_size):
        super().__init__(config, block_size)
        self._byte_correction = 0.0
        self._disk_byte_correction = [0.0] * self.c["disks"]
        if self.policy != "max_min_fair":
            raise ValueError("grouped reads require max_min_fair")
        self._invalidate_rates()

    def _invalidate_rates(self):
        # Remaining bytes change continuously; rates only change when the
        # active set changes. Storage configuration is fixed for this instance.
        self._cached_raw_rates = None
        self._cached_rates = None
        self._next_ready = math.inf

    def _account_transfer(self, disk, transferred):
        # Kahan summation only changes diagnostic byte counters, not read state.
        value = transferred - self._byte_correction
        total = self.bytes_transferred + value
        self._byte_correction = (total - self.bytes_transferred) - value
        self.bytes_transferred = total
        value = transferred - self._disk_byte_correction[disk]
        total = self.disk_bytes_transferred[disk] + value
        self._disk_byte_correction[disk] = (total - self.disk_bytes_transferred[disk]) - value
        self.disk_bytes_transferred[disk] = total

    def submit(self, now, key, blocks, demand=None):
        assert abs(now - self.now) < 1e-9 and key not in self.pending and blocks
        assert all(block in self.present for block in blocks)
        assert demand and demand > 0, "max_min_fair requires a positive demand"
        # Membership/hit validation still examines every block. Single-disk
        # placement is constant; multi-disk placement still hashes every block.
        if self.c["disks"] == 1:
            counts = {0: len(blocks)}
        else:
            counts = defaultdict(int)
            for block in blocks:
                counts[self.disk_of(block)] += 1
        self.pending[key] = len(blocks)
        for disk, count in counts.items():
            first_path = self.path_seq[disk]
            self.path_seq[disk] += count
            self.disk_bytes_submitted[disk] += count * self.block_bytes
            self.queues[(disk, first_path)].append(ReadGroup(
                key, self.block_bytes, now + self.c["read_latency_s"], demand, count))
        self._invalidate_rates()

    def _group_raw_rates(self):
        if self._cached_raw_rates is not None and self._next_ready > self.now + 1e-12:
            return self._cached_raw_rates
        self._invalidate_rates()
        by_disk = defaultdict(list)
        for path, queue in self.queues.items():
            read = queue[0]
            if read.ready <= self.now + 1e-12:
                by_disk[path[0]].append((path, read))
            else:
                self._next_ready = min(self._next_ready, read.ready)
        raw = {}
        for disk, items in by_disk.items():
            alloc = weighted_waterfill(self.c["disk_Bps"],
                [r.demand for _, r in items], [r.block_count for _, r in items])
            raw.update(zip((p for p, _ in items), alloc))
        self._cached_raw_rates = raw
        return raw

    def submit_counted(self, now, key, counts, block_bytes, demand, latency):
        """Trusted layer service reuses validated disk counts, not arbitrary client blocks."""
        if (now != self.now or key in self.pending or not counts
                or not all(math.isfinite(x) for x in (block_bytes,demand,latency))
                or block_bytes <= 0 or demand <= 0 or latency < 0
                or not all(type(d) is int and 0<=d<self.c["disks"]
                           and type(n) is int and n>0 for d,n in counts.items())):
            raise ValueError("invalid counted layer read")
        self.pending[key] = sum(counts.values())
        for disk,count in sorted(counts.items()):
            path = self.path_seq[disk]
            self.path_seq[disk] += count
            self.disk_bytes_submitted[disk] += count*block_bytes
            self.queues[(disk,path)].append(ReadGroup(key,block_bytes,now+latency,demand,count,True))
        self._invalidate_rates()

    def _group_rates(self):
        raw = self._group_raw_rates()
        if self._cached_rates is None:
            total = sum(rate * self.queues[p][0].block_count for p, rate in raw.items())
            scale = self._link_scale(total)
            self._cached_rates = {p: rate * scale for p, rate in raw.items()}
        return self._cached_rates

    def _expand_rates(self, rates):
        return {(disk, p): rate for (disk, start), rate in rates.items()
                for p in range(start, start + self.queues[(disk, start)][0].block_count)}

    def _raw_rates(self):
        return self._expand_rates(self._group_raw_rates())

    def rates(self):
        return self._expand_rates(self._group_rates())

    def link_flux(self):
        raw = self._group_raw_rates()
        demand = sum(rate * self.queues[p][0].block_count for p, rate in raw.items())
        return demand, demand * self._link_scale(demand)

    def next_time(self):
        return self._next_time_for_rates(self._group_rates())

    def _next_time_for_rates(self, rates):
        target = super()._next_time_for_rates(rates)
        if (target == self.now and rates
                and all(self.queues[p][0].remaining >
                        (0. if self.queues[p][0].complete_exactly else 1e-5) for p in rates)):
            # A positive remaining/rate can round away when added to now.
            # Move one representable clock value forward; retain all bytes
            # and account their transfer in advance(), rather than dropping
            # residual bytes or looping forever at the same timestamp.
            return math.nextafter(self.now, math.inf)
        return target

    def advance(self, target):
        rates = self._group_rates()
        if target < self.now - 1e-12 or target > self._next_time_for_rates(rates) + 1e-9:
            raise ValueError("must stop at next storage event")
        by_disk = defaultdict(float)
        for path, rate in rates.items():
            read = self.queues[path][0]
            by_disk[path[0]] += rate * read.block_count
            per_block = min(read.remaining, rate * (target - self.now))
            read.remaining -= per_block
            transferred = per_block * read.block_count
            self._account_transfer(path[0], transferred)
        for disk, rate in by_disk.items():
            self.disk_peak_Bps[disk] = max(self.disk_peak_Bps[disk], rate)
        self.now = target
        done, completed_groups = [], []
        for path, queue in self.queues.items():
            read = queue[0]
            if read.remaining <= (0. if read.complete_exactly else 1e-5):
                self.pending[read.key] -= read.block_count
                if not self.pending[read.key]:
                    del self.pending[read.key]
                    done.append(read.key)
                completed_groups.append(path)
        for path in completed_groups:
            del self.queues[path]
        if completed_groups:
            self._invalidate_rates()
        return done

    def snapshot(self):
        rates = self._group_rates()
        disk_rates = [0.] * self.c["disks"]
        for path, rate in rates.items():
            disk_rates[path[0]] += rate * self.queues[path][0].block_count
        return dict(
            placement="hrw_sha256_content_hash", disks=self.c["disks"],
            disk_Bps=self.c["disk_Bps"], shared_link_Bps=self.link_Bps,
            disk_rates_Bps=disk_rates, disk_peak_Bps=self.disk_peak_Bps.copy(),
            disk_bytes_submitted=self.disk_bytes_submitted.copy(),
            disk_bytes_transferred=self.disk_bytes_transferred.copy(), timestamp_s=self.now,
            queued_reads=sum(q[0].block_count for q in self.queues.values()),
            active_paths=sum(self.queues[p][0].block_count for p in rates),
            bytes_remaining=sum(q[0].remaining * q[0].block_count for q in self.queues.values()),
            path_rates_Bps={f"{d}:{p}": rate for (d, p), rate in self._expand_rates(rates).items()})
