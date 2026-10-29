"""HRW placement and separate disk budgets; times are analytically checked."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from sim.storage import Storage


def store(**overrides):
    config = dict(disks=3, paths_per_disk=4, disk_Bps=40,
                  shared_link_Bps=None, path_Bps=1000,
                  kv_bytes_per_token=100, read_latency_s=0,
                  bandwidth_policy="max_min_fair")
    config.update(overrides)
    return Storage(config, 1)


def blocks_for(s, disk, count):
    blocks = []
    for i in range(10000):
        b = hashlib.sha256(f"candidate:{i}".encode()).digest()
        if s.disk_of(b) == disk:
            blocks.append(b)
            if len(blocks) == count:
                return blocks
    raise AssertionError("insufficient hashes")


class DiskStorageTests(unittest.TestCase):
    def test_three_disks_parallel_and_no_global_pool(self):
        # 3 x 100B, evenly placed => 100/40 = 2.5s, aggregate 120B/s.
        s = store()
        blocks = [blocks_for(s, d, 1)[0] for d in range(3)]
        s.present.update(blocks)
        s.submit(0, (0, "r"), blocks, 1)
        self.assertEqual(sum(s.rates().values()), 120)
        self.assertAlmostEqual(s.next_time(), 2.5)
        self.assertEqual(s.advance(2.5), [(0, "r")])
        self.assertEqual(s.disk_bytes_transferred, [100, 100, 100])
        self.assertEqual(s.disk_peak_Bps, [40, 40, 40])

    def test_hot_disk_cannot_borrow_idle_disk_bandwidth(self):
        # Same 300B all on disk0 => 300/40 = 7.5s, not 300/120.
        s = store()
        blocks = blocks_for(s, 0, 3)
        s.present.update(blocks)
        s.submit(0, (0, "hot"), blocks, 1)
        self.assertEqual(sum(s.rates().values()), 40)
        self.assertAlmostEqual(s.next_time(), 7.5)
        self.assertEqual(s.advance(7.5), [(0, "hot")])
        self.assertEqual(s.disk_bytes_submitted, [300, 0, 0])

    def test_unified_pool_removes_hash_hotspot_penalty(self):
        # 同一批块：旧三盘模式全部落盘0，仅40B/s；统一池可用全部120B/s。
        topology = store()
        hot = blocks_for(topology,0,3)
        balanced = [blocks_for(topology,d,1)[0] for d in range(3)]
        for blocks in (hot,balanced):
            pooled = store(disks=1,disk_Bps=120)
            pooled.present.update(blocks)
            pooled.submit(0,(0,"r"),blocks,1)
            self.assertAlmostEqual(sum(pooled.rates().values()),120)
            self.assertAlmostEqual(pooled.next_time(),2.5)  # 300B / 120B/s
            self.assertEqual(pooled.advance(2.5),[(0,"r")])
            self.assertEqual(pooled.disk_bytes_transferred,[300])

    def test_request_finishes_only_after_slowest_disk(self):
        # Disk byte vector [300,200,100]. Finishing a disk is not request ready.
        s = store()
        blocks = sum((blocks_for(s, d, n) for d, n in enumerate([3, 2, 1])), [])
        s.present.update(blocks)
        s.submit(0, (1, "r"), blocks, 1)
        self.assertEqual(s.advance(2.5), [])
        self.assertIn((1, "r"), s.pending)
        self.assertEqual(s.advance(5), [])
        self.assertEqual(s.advance(7.5), [(1, "r")])
        self.assertEqual(s.disk_bytes_transferred, [300, 200, 100])

    def test_new_read_changes_only_its_disk_and_conserves_bytes(self):
        s = store()
        a, b = blocks_for(s, 0, 2)
        c = blocks_for(s, 1, 1)[0]
        s.present.update([a, b, c])
        s.submit(0, (0, "a"), [a], 100)
        s.submit(0, (0, "c"), [c], 100)
        s.advance(1)
        s.submit(1, (1, "b"), [b], 100)
        rates = s.snapshot()["disk_rates_Bps"]
        self.assertEqual(rates, [40, 40, 0])
        self.assertEqual(s.advance(2.5), [(0, "c")])
        self.assertEqual(s.advance(4), [(0, "a")])
        self.assertEqual(s.advance(5), [(1, "b")])
        self.assertAlmostEqual(s.bytes_transferred, 300)

    def test_hash_mapping_is_stable_across_processes_and_disk_addition(self):
        code = ("import json;from sim.storage import Storage;"
                "print(json.dumps([Storage.disk_of(type('S',(),{'c':{'disks':3}})(),"
                "i.to_bytes(4,'little')) for i in range(100)]))")
        mappings = [subprocess.check_output(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, PYTHONHASHSEED=seed), text=True)
            for seed in ("0", "71")]
        self.assertEqual(mappings[0], mappings[1])
        three, four = store(), store(disks=4)
        for i, expected in enumerate(json.loads(mappings[0])):
            b = i.to_bytes(4, "little")
            self.assertEqual(three.disk_of(b), expected)
            self.assertIn(four.disk_of(b), (expected, 3))

    def test_legacy_shared_link_still_limits_explicit_configs(self):
        s = store(shared_link_Bps=30)
        blocks = [blocks_for(s, d, 1)[0] for d in range(3)]
        s.present.update(blocks)
        s.submit(0, (0, "r"), blocks, 1)
        self.assertAlmostEqual(sum(s.rates().values()), 30)
        self.assertEqual(s.advance(10), [(0, "r")])

    def test_invalid_link_rejected_and_missing_link_disabled(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                store(shared_link_Bps=value)
        c = store().c.copy()
        del c["shared_link_Bps"]
        self.assertIsNone(Storage(c, 1).link_Bps)

    def test_default_stack_config_has_independent_three_disks(self):
        from sim.request_profile import normalize_config
        path = Path(__file__).resolve().parents[1]/"configs/tfs_stack_prefill.json"
        c = json.loads(path.read_text())
        del c["admission"]["reference_bandwidth_Bps"]
        c = normalize_config(c)
        self.assertEqual(c["storage"]["disks"], 3)
        self.assertEqual(c["storage"]["disk_Bps"], 40e9)
        self.assertIsNone(c["storage"]["shared_link_Bps"])
        self.assertEqual(c["admission"]["reference_bandwidth_Bps"], 120e9)


if __name__ == "__main__":
    unittest.main()
