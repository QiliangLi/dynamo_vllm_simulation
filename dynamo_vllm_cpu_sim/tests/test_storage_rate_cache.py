"""Cached allocations must preserve the uncached grouped model exactly."""
from collections import defaultdict
from copy import deepcopy
import math
import random
import unittest
from unittest.mock import patch

from sim.grouped_storage import GroupedStorage, weighted_waterfill


class UncachedStorage(GroupedStorage):
    """Original allocation calculation; independent of cache validity logic."""
    def _group_raw_rates(self):
        by_disk = defaultdict(list)
        for path, queue in self.queues.items():
            read = queue[0]
            if read.ready <= self.now + 1e-12:
                by_disk[path[0]].append((path, read))
        raw = {}
        for disk, items in by_disk.items():
            alloc = weighted_waterfill(self.c['disk_Bps'],
                [r.demand for _, r in items], [r.block_count for _, r in items])
            raw.update(zip((p for p, _ in items), alloc))
        return raw

    def _group_rates(self):
        raw = self._group_raw_rates()
        total = sum(rate * self.queues[p][0].block_count for p, rate in raw.items())
        scale = self._link_scale(total)
        return {p: rate * scale for p, rate in raw.items()}


def config(link=None):
    return dict(disks=4, paths_per_disk=2, disk_Bps=2500., path_Bps=1000.,
        shared_link_Bps=link, kv_bytes_per_token=100, read_latency_s=.001,
        bandwidth_policy='max_min_fair')


class RateCacheTest(unittest.TestCase):
    def test_queries_reuse_allocations_but_submission_and_completion_refresh(self):
        s = GroupedStorage(config(), 1)
        s.submit_counted(0., (0, 'a'), {0: 2, 3: 1}, 100., 50., 0.)
        with patch('sim.grouped_storage.weighted_waterfill', wraps=weighted_waterfill) as allocate:
            rates = s.rates()
            self.assertEqual(allocate.call_count, 2)
            # Repeated polling, snapshots and partial advancement do not change shares.
            for _ in range(5):
                s.next_time(); s.snapshot(); s.link_flux()
                self.assertEqual(s.rates(), rates)
            s.advance(.001)
            self.assertEqual(s.rates(), rates)
            self.assertEqual(allocate.call_count, 2)
            s.submit_counted(s.now, (1, 'b'), {0: 1}, 100., 5000., 0.)
            self.assertNotEqual(s.rates(), rates)
            self.assertEqual(allocate.call_count, 4)
            s.advance(s.next_time())
            s.rates()
            self.assertGreater(allocate.call_count, 4)

    def test_latency_expiry_refreshes_without_another_submission(self):
        s = GroupedStorage(config(link=3000.), 1)
        s.submit_counted(0., (0, 'a'), {0: 1}, 100., 100., 0.)
        s.submit_counted(0., (1, 'b'), {1: 1}, 100., 100., .001)
        self.assertEqual(s.rates(), {(0, 0): 2500.})
        self.assertEqual(s.next_time(), .001)
        s.advance(.0005)
        self.assertEqual(s.rates(), {(0, 0): 2500.})
        s.advance(.001)
        self.assertEqual(s.rates(), {(0, 0): 1500., (1, 0): 1500.})
        # Returned public maps may be edited without corrupting the cache.
        s.rates().clear()
        self.assertEqual(s.snapshot()['disk_rates_Bps'], [1500., 1500., 0., 0.])

    def test_bulk_submission_invalidates_empty_cache(self):
        s = GroupedStorage(config(), 1)
        self.assertEqual(s.rates(), {})
        self.assertEqual(s.next_time(), math.inf)
        s.present.add(b'a')
        s.submit(0., (0, 'bulk'), [b'a', b'a'], 50.)
        self.assertEqual(s.rates(), {})
        s.advance(s.next_time())
        self.assertEqual(sum(s.rates().values()), 2500.)
        self.assertEqual(s.advance(s.next_time()), [(0, 'bulk')])
        self.assertEqual(s.rates(), {})
        self.assertEqual(s.next_time(), math.inf)

    def test_counted_event_traces_match_uncached_model_bit_for_bit(self):
        for seed in (0, 17, 418):
            for link in (None, 3000.):
                with self.subTest(seed=seed, link=link):
                    rng = random.Random(seed)
                    stores = [cls(config(link), 1) for cls in (UncachedStorage, GroupedStorage)]

                    def check():
                        a, b = stores
                        self.assertEqual(a.rates(), b.rates())
                        self.assertEqual(a.next_time(), b.next_time())
                        self.assertEqual(a.link_flux(), b.link_flux())
                        self.assertEqual(a.snapshot(), b.snapshot())
                        self.assertEqual(a.pending, b.pending)

                    def advance(t):
                        self.assertEqual(stores[0].advance(t), stores[1].advance(t))
                        check()

                    for i, arrival in enumerate(sorted([0., 0., .001, .001] +
                            [rng.random() * .2 for _ in range(26)])):
                        while stores[0].next_time() <= arrival:
                            advance(stores[0].next_time())
                        advance(arrival)
                        counts = {d: rng.randint(1, 15) for d in range(4) if rng.random() < .7} or {0: 1}
                        demand = rng.choice([1., 200., 1e5])
                        latency = rng.choice([0., .001, .003])
                        for s in stores:
                            s.submit_counted(s.now, (i % 12, str(i)), deepcopy(counts),
                                160., demand, latency)
                        check()
                    while stores[0].pending:
                        advance(stores[0].next_time())
                    self.assertFalse(stores[1].pending)


if __name__ == '__main__':
    unittest.main()
