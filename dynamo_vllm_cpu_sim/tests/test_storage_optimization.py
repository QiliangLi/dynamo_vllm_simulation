"""Exact event-by-event comparison against the committed pre-optimization model.

Run with --output <new directory> to save the checked scenarios and fingerprints.
The reference is the actual source frozen by step 1, not a second implementation
of the optimized formulas. Wall-clock performance is measured separately.
"""
import argparse
import copy
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import random
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sim.storage import Storage

REFERENCE = ROOT / "tests/fixtures/storage_before_step2"
EVIDENCE = []


def frozen_module(relative, name):
    path = REFERENCE / "runtime_sources" / relative
    expected = json.loads((REFERENCE / "manifest.json").read_text())["source_sha256"][relative]
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise AssertionError("frozen reference source changed")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OldStorage = frozen_module("sim/storage.py", "storage_before_step2").Storage


def config(policy="max_min_fair", disks=1, link=None, latency=0.):
    return dict(disks=disks, paths_per_disk=2, disk_Bps=1200,
                path_Bps=450, shared_link_Bps=link, kv_bytes_per_token=13,
                read_latency_s=latency, bandwidth_policy=policy)


def state(store):
    return dict(snapshot=store.snapshot(), rates=store.rates(),
                flux=store.link_flux(), next_time=store.next_time(),
                pending=store.pending.copy(), path_seq=dict(store.path_seq),
                active_queues=[(path, [(r.key, r.remaining, r.ready, r.demand) for r in q])
                               for path, q in store.queues.items() if q])


def compare_scenario(test, cfg, block_size, seed):
    rng = random.Random(seed)
    stores = [cls(copy.deepcopy(cfg), block_size) for cls in (OldStorage, Storage)]
    blocks = [hashlib.sha256(f"block:{i}".encode()).digest() for i in range(29)]
    for store in stores:
        store.present.update(blocks)
    # Include simultaneous arrivals, in-flight arrivals, idle gaps and path reuse.
    arrivals = sorted([0., 0., .00004, .00004, .003, .03, 20., 20., 40., 40.]
                      + [rng.random() * .5 for _ in range(10)])
    events, done_count, expected_bytes = [], 0, 0

    def check(kind, done=None):
        before, after = map(state, stores)
        test.assertEqual(before, after)  # exact floats, order and path IDs
        digest = hashlib.sha256(repr(before).encode()).hexdigest()
        events.append(dict(kind=kind, now_s=stores[0].now,
                           state_sha256=digest, done=done))

    def advance(target):
        nonlocal done_count
        done = [store.advance(target) for store in stores]
        test.assertEqual(done[0], done[1])  # simultaneous completion order too
        done_count += len(done[0])
        check("advance", done[0])

    check("initial")
    for i, arrival in enumerate(arrivals):
        while stores[0].next_time() <= arrival:
            advance(stores[0].next_time())
        if stores[0].now < arrival:
            advance(arrival)
        selected = [rng.choice(blocks) for _ in range(rng.randint(1, 12))]
        demand = rng.choice([1., 30., 750., 5000.])
        for store in stores:
            store.submit(arrival, (i % 4, f"r{i}"), selected, demand)
        expected_bytes += len(selected) * block_size * cfg["kv_bytes_per_token"]
        check("submit")
    while stores[0].pending:
        test.assertTrue(math.isfinite(stores[0].next_time()))
        advance(stores[0].next_time())
    test.assertEqual(done_count, len(arrivals))
    test.assertAlmostEqual(stores[1].bytes_transferred, expected_bytes, delta=1e-7)
    if cfg["bandwidth_policy"] == "max_min_fair":
        test.assertEqual(len(stores[1].queues), 0)
        test.assertGreater(len(stores[0].queues), 0)
    else:
        test.assertEqual(list(stores[0].queues), list(stores[1].queues))
        test.assertLessEqual(len(stores[1].queues), cfg["disks"] * cfg["paths_per_disk"])
    return dict(config=cfg, block_size=block_size, seed=seed, events=events,
                requests=len(arrivals), bytes=expected_bytes,
                reference_queue_entries=len(stores[0].queues),
                optimized_queue_entries=len(stores[1].queues),
                finish_s=stores[1].now)


class StorageOptimizationTest(unittest.TestCase):
    def test_event_matrix_exactly_matches_frozen_reference(self):
        EVIDENCE.clear()
        for policy, disks, link, latency, block_size, seed in itertools.product(
                ("active_split", "max_min_fair"), (1, 3), (None, 300.),
                (0., .001), (1, 4), (0, 17, 418)):
            with self.subTest(policy=policy, disks=disks, link=link,
                              latency=latency, block_size=block_size, seed=seed):
                EVIDENCE.append(compare_scenario(
                    self, config(policy, disks, link, latency), block_size, seed))

    def test_single_disk_skips_hash_and_multi_disk_keeps_hrw(self):
        single = Storage(config(), 1)
        with mock.patch("sim.storage.hashlib.sha256", side_effect=AssertionError("unneeded hash")):
            self.assertEqual(single.disk_of(b"a"), 0)
        for disks in (2, 3, 4):
            before, after = OldStorage(config(disks=disks), 1), Storage(config(disks=disks), 1)
            for i in range(300):
                block = i.to_bytes(8, "little")
                self.assertEqual(before.disk_of(block), after.disk_of(block))

    def test_maxmin_retired_paths_do_not_accumulate(self):
        store = Storage(config(), 1)
        store.present.add(b"a")
        for i in range(100):
            store.submit(store.now, (0, str(i)), [b"a"] * 16, 100.)
            self.assertEqual(len(store.queues), 16)
            while store.pending:
                store.advance(store.next_time())
            self.assertEqual(len(store.queues), 0)
        self.assertEqual(store.path_seq[0], 1600)

    def test_service_history_and_completion_feedback_match_reference(self):
        from sim.stack.storage_service import Service
        def comparable_stats(service):
            stats = service.op_stats()
            if isinstance(service, Service):
                # 旧快照没有GC控制元数据；逐项验证后保留其余字段的严格相等对拍。
                window = stats.pop('gc_window')
                self.assertEqual(window['policy'], 'automatic')
                self.assertFalse(window['active'])
                self.assertFalse(window['failure'])
                self.assertEqual(window['memory_samples'], 0)
            return stats
        old = frozen_module("sim/stack/storage_service.py", "service_before_step2")
        old.Storage = OldStorage
        for disks, read_log in itertools.product((1, 3), (False, True)):
            cfg = dict(block_size=1, storage=config(disks=disks, latency=.001))
            cfg["storage"]["record_reads"] = read_log
            # This suite verifies the exact per-block implementation from step 2.
            # Grouped representation has a separate numeric-equivalence suite.
            cfg["storage"]["max_min_read_representation"] = "per_block"
            with mock.patch("time.monotonic", return_value=100.):
                services = [cls(copy.deepcopy(cfg), []) for cls in (old.Service, Service)]
            blocks = [bytes([i]) for i in (1, 2, 1, 3, 4)]
            for service in services:
                service.store.present.update(blocks)
            for i, elapsed in enumerate((0., 0., .0003, .03, 1.)):
                with mock.patch("time.monotonic", return_value=100. + elapsed):
                    for service in services:
                        service.op_submit(i % 2, f"r{i}", [b.hex() for b in blocks], 800.)
                    self.assertEqual(comparable_stats(services[0]), comparable_stats(services[1]))
            with mock.patch("time.monotonic", return_value=103.):
                for worker in (0, 1):
                    self.assertEqual(services[0].op_finished(worker), services[1].op_finished(worker))
                self.assertEqual(comparable_stats(services[0]), comparable_stats(services[1]))
            # Log statistics reuse placement counters, not a second disk pass.
            with mock.patch("time.monotonic", return_value=104.), mock.patch.object(
                    services[1].store, "disk_of", wraps=services[1].store.disk_of) as placement:
                services[1].op_submit(0, "count", [b.hex() for b in blocks], 800.)
                self.assertEqual(placement.call_count, len(blocks))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StorageOptimizationTest))
    if args.output:
        files = [Path(__file__), ROOT / "sim/storage.py", ROOT / "sim/stack/storage_service.py"]
        for source in files:
            target = args.output / "runtime_sources" / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        (args.output / "events.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in EVIDENCE))
        (args.output / "report.json").write_text(json.dumps(dict(
            passed=result.wasSuccessful(), tests=result.testsRun, scenarios=len(EVIDENCE),
            checked_states=sum(len(case["events"]) for case in EVIDENCE),
            reference="tests/fixtures/storage_before_step2/runtime_sources",
            reference_sha256=json.loads((REFERENCE / "manifest.json").read_text())["source_sha256"],
            source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in files},
            boundary="Exact model-time equivalence; wall-clock outcomes need not match."
        ), indent=2) + "\n")
    sys.exit(not result.wasSuccessful())
