"""Weighted reads versus the frozen, actual per-block model from step 2."""
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
from sim.grouped_storage import GroupedStorage, weighted_waterfill
from sim.storage import Storage, waterfill

REFERENCE = ROOT / "tests/fixtures/storage_before_grouping"
EVIDENCE = []
PRECISION_EVIDENCE = {}
# Aggregating sums changes floating-point evaluation order. These are explicit
# numerical budgets, not claims of bitwise equivalence or changes to the model.
BUDGETS = dict(time=(1e-10, 1e-11), rate=(1e-6, 1e-10),
               bytes=(1e-3, 1e-12), per_block_bytes=(1e-6, 1e-12))


def frozen(relative, name):
    path = REFERENCE / "runtime_sources" / relative
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == json.loads((REFERENCE / "manifest.json").read_text())["source_sha256"][relative]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OldStorage = frozen("sim/storage.py", "storage_before_step3").Storage


def config(disks=1, link=None, latency=0.):
    return dict(disks=disks, paths_per_disk=2, disk_Bps=1200.,
                path_Bps=450., shared_link_Bps=link, kv_bytes_per_token=13,
                read_latency_s=latency, bandwidth_policy="max_min_fair")


def compare_scenario(test, cfg, block_size, seed, heavy=False):
    rng = random.Random(seed)
    stores = [cls(copy.deepcopy(cfg), block_size) for cls in (OldStorage, GroupedStorage)]
    blocks = [hashlib.sha256(f"block:{i}".encode()).digest() for i in range(43)]
    for store in stores:
        store.present.update(blocks)
    arrivals = sorted([0., 0., .00004, .00004, .003, .03, 20., 20., 40., 40.]
                      + [rng.random() * .5 for _ in range(10)])
    maxima = {kind: 0. for kind in BUDGETS}
    events, completions = [], []
    peak_objects = [0, 0]
    expected_bytes = 0
    old_stalled_events = []

    def close(x, y, kind):
        if math.isinf(x) or math.isinf(y):
            test.assertEqual(x, y)
            return
        maxima[kind] = max(maxima[kind], abs(x - y))
        absolute, relative = BUDGETS[kind]
        if not math.isclose(x, y, abs_tol=absolute, rel_tol=relative):
            test.fail(f"{kind}: {x!r} != {y!r}, now={stores[0].now}")

    def check(kind, done=None):
        before, after = stores
        close(before.now, after.now, "time")
        close(before.next_time(), after.next_time(), "time")
        test.assertEqual(before.pending, after.pending)
        test.assertEqual(before.path_seq, after.path_seq)
        old_rates, new_rates = before.rates(), after.rates()
        test.assertEqual(old_rates.keys(), new_rates.keys())
        for path in old_rates:
            close(old_rates[path], new_rates[path], "rate")
        old_snap, new_snap = before.snapshot(), after.snapshot()
        for key in ("placement", "disks", "disk_Bps", "shared_link_Bps", "queued_reads", "active_paths",
                    "disk_bytes_submitted"):
            test.assertEqual(old_snap[key], new_snap[key])
        for key in ("disk_rates_Bps", "disk_peak_Bps"):
            for x, y in zip(old_snap[key], new_snap[key]):
                close(x, y, "rate")
        for x, y in zip(before.link_flux(), after.link_flux()):
            close(x, y, "rate")
        close(before.bytes_transferred, after.bytes_transferred, "bytes")
        close(old_snap["bytes_remaining"], new_snap["bytes_remaining"], "bytes")
        for x, y in zip(before.disk_bytes_transferred, after.disk_bytes_transferred):
            close(x, y, "bytes")
        logical_reads = {}
        for (disk, start), queue in after.queues.items():
            read = queue[0]
            for path in range(start, start + read.block_count):
                logical_reads[(disk, path)] = read
        test.assertEqual(before.queues.keys(), logical_reads.keys())
        for path, queue in before.queues.items():
            old_read, new_read = queue[0], logical_reads[path]
            test.assertEqual((old_read.key, old_read.demand), (new_read.key, new_read.demand))
            close(old_read.ready, new_read.ready, "time")
            close(old_read.remaining, new_read.remaining, "per_block_bytes")
        for i, store in enumerate(stores):
            peak_objects[i] = max(peak_objects[i], len(store.queues))
        events.append(dict(kind=kind, now_s=before.now, done=done,
                           logical_blocks=sum(after.pending.values()),
                           per_block_objects=len(before.queues), grouped_objects=len(after.queues),
                           max_absolute_errors=maxima.copy()))

    def advance(target):
        test.assertLess(len(events), 2048, "event loop did not make bounded progress")
        if stores[0].pending and stores[0].next_time() == stores[0].now and target > stores[0].now:
            old_stalled_events.append(dict(now_s=stores[0].now, advanced_to_s=target,
                                           delta_s=target-stores[0].now))
        done = [store.advance(target) for store in stores]
        test.assertEqual(done[0], done[1])  # order matters for simultaneous feedback
        completions.extend((target, key) for key in done[0])
        check("advance", done[0])

    check("initial")
    for i, arrival in enumerate(arrivals):
        while min(s.next_time() for s in stores) <= arrival:
            times = [s.next_time() for s in stores]
            close(*times, "time")
            # Coalesce only the small roundoff difference between predictions;
            # both models allow stopping up to 1e-9s beyond their next event.
            advance(max(times))
        if stores[0].now < arrival:
            advance(arrival)
        size = rng.choice([1, 7, 64, 511, 512, 1024]) if heavy else rng.randint(1, 12)
        selected = [rng.choice(blocks) for _ in range(size)]
        demand = rng.choice([1., 30., 750., 5000.])
        for store in stores:
            store.submit(arrival, (i % 4, f"r{i}"), selected, demand)
        expected_bytes += len(selected) * block_size * cfg["kv_bytes_per_token"]
        check("submit")
    while stores[0].pending or stores[1].pending:
        times = [s.next_time() for s in stores]
        close(*times, "time")
        test.assertTrue(all(math.isfinite(t) for t in times))
        advance(max(times))
    test.assertEqual(len(completions), len(arrivals))
    test.assertEqual(len(stores[1].queues), 0)
    test.assertAlmostEqual(stores[1].bytes_transferred, expected_bytes, delta=1e-3)
    return dict(config=cfg, block_size=block_size, seed=seed, heavy=heavy,
                requests=len(arrivals), bytes=expected_bytes, events=events,
                old_stalled_events=old_stalled_events,
                peak_objects=peak_objects, max_absolute_errors=maxima,
                completions=completions, finish_s=stores[1].now)


class GroupedStorageTest(unittest.TestCase):
    def test_dynamic_events_match_frozen_per_block_model(self):
        EVIDENCE.clear()
        for disks, link, latency, block_size, seed in itertools.product(
                (1, 3), (None, 300.), (0., .001), (1, 4), (0, 17, 418)):
            with self.subTest(disks=disks, link=link, latency=latency, block_size=block_size, seed=seed):
                EVIDENCE.append(compare_scenario(self, config(disks, link, latency), block_size, seed))
        # Include production-scale byte/rate magnitudes and asymmetric counts.
        for disks, seed in itertools.product((1, 3), (0, 418)):
            cfg = config(disks, latency=.0001)
            cfg.update(disk_Bps=120e9 / disks, kv_bytes_per_token=109824)
            EVIDENCE.append(compare_scenario(self, cfg, 1, seed, heavy=True))

    def test_weighted_filling_matches_expanded_demands(self):
        rng = random.Random(418)
        cases = [(40., [1., 8., 100.], [3, 2, 4]),
                 (120., [1., 1.], [512, 1]), (120., [1., 2.], [3, 2])]
        for _ in range(300):
            count = rng.randint(1, 8)
            cases.append((10. ** rng.randint(0, 8),
                          [10. ** rng.randint(-3, 9) for _ in range(count)],
                          [rng.randint(1, 80) for _ in range(count)]))
        for capacity, demands, weights in cases:
            expanded = waterfill(capacity, [d for d, w in zip(demands, weights) for _ in range(w)])
            grouped = weighted_waterfill(capacity, demands, weights)
            offset = 0
            for rate, weight in zip(grouped, weights):
                for expected in expanded[offset:offset + weight]:
                    self.assertTrue(math.isclose(rate, expected, rel_tol=1e-10, abs_tol=1e-6))
                offset += weight
        self.assertEqual(weighted_waterfill(513., [1000., 1000.], [512, 1]), [1., 1.])

    def test_internal_events_do_not_expand_paths(self):
        store = GroupedStorage(config(), 1)
        store.present.add(b"a")
        store.submit(0., (0, "r"), [b"a"] * 512, 1.)
        self.assertEqual(len(store.queues), 1)
        self.assertEqual(store.snapshot()["queued_reads"], 512)
        self.assertEqual(store.snapshot()["active_paths"], 512)
        with mock.patch.object(store, "_expand_rates", side_effect=AssertionError("expanded")):
            self.assertEqual(store.advance(store.next_time()), [(0, "r")])
        self.assertEqual(store.bytes_transferred, 512 * 13)

    def test_clock_roundoff_makes_progress_without_discarding_bytes(self):
        # This stalls the actual frozen old model at 40.0001009152s.
        cfg = config(latency=.0001)
        cfg.update(disk_Bps=120e9, kv_bytes_per_token=109824)
        old, new = OldStorage(copy.deepcopy(cfg), 1), GroupedStorage(cfg, 1)
        for store in (old, new):
            store.now = 40.
            store.present.add(b"a")
            store.submit(40., (0, "r"), [b"a"], 5000.)
            store.advance(store.next_time())  # startup latency
            store.advance(store.next_time())  # rounded transfer completion
        self.assertEqual(old.next_time(), old.now)
        self.assertGreater(new.next_time(), new.now)
        self.assertEqual(new.next_time(), math.nextafter(new.now, math.inf))
        PRECISION_EVIDENCE.update(old_now_s=old.now, old_next_s=old.next_time(),
                                  old_remaining_bytes=old.queues[(0, 0)][0].remaining,
                                  new_next_s=new.next_time(), clock_increment_s=new.next_time()-new.now)
        self.assertEqual(new.advance(new.next_time()), [(0, "r")])
        self.assertEqual(new.bytes_transferred, 109824)
        self.assertEqual(new.snapshot()["bytes_remaining"], 0)
        PRECISION_EVIDENCE["new_transferred_bytes"] = new.bytes_transferred

    def test_service_stats_and_completion_feedback_match_reference(self):
        from sim.stack.storage_service import Service
        module = frozen("sim/stack/storage_service.py", "service_before_step3")
        module.Storage = OldStorage

        def compare(x, y, key=""):
            if key == "" and isinstance(y, dict) and 'gc_window' in y:
                # 冻结服务早于GC窗口功能；独立验证新增控制元数据，再对拍原存储状态。
                y = dict(y)
                window = y.pop('gc_window')
                self.assertEqual(window['policy'], 'automatic')
                self.assertFalse(window['active'])
                self.assertFalse(window['failure'])
                self.assertEqual(window['memory_samples'], 0)
            if isinstance(x, dict):
                self.assertEqual(x.keys(), y.keys())
                for k in x:
                    compare(x[k], y[k], key + "/" + str(k))
            elif isinstance(x, list):
                self.assertEqual(len(x), len(y))
                for a, b in zip(x, y):
                    compare(a, b, key)
            elif isinstance(x, float):
                kind = "rate" if "Bps" in key else "bytes" if "bytes" in key else "time"
                absolute, relative = BUDGETS[kind]
                self.assertTrue(math.isclose(x, y, abs_tol=absolute, rel_tol=relative), key)
            else:
                self.assertEqual(x, y, key)

        for disks, read_log in itertools.product((1, 3), (False, True)):
            cfg = dict(block_size=1, storage=config(disks, latency=.001))
            cfg["storage"]["record_reads"] = read_log
            with mock.patch("time.monotonic", return_value=100.):
                services = [cls(copy.deepcopy(cfg), []) for cls in (module.Service, Service)]
            blocks = [bytes([i]) for i in (1, 2, 1, 3, 4)]
            for service in services:
                service.store.present.update(blocks)
            for i, elapsed in enumerate((0., 0., .0003, .03, 1.)):
                with mock.patch("time.monotonic", return_value=100. + elapsed):
                    for service in services:
                        service.op_submit(i % 2, f"r{i}", [b.hex() for b in blocks], 800.)
                    compare(services[0].op_stats(), services[1].op_stats())
            with mock.patch("time.monotonic", return_value=103.):
                for worker in (0, 1):
                    self.assertEqual(services[0].op_finished(worker), services[1].op_finished(worker))
                compare(services[0].op_stats(), services[1].op_stats())

    def test_service_uses_grouped_maxmin_and_preserves_optout_and_fifo(self):
        from sim.stack.storage_service import Service
        for policy, representation, expected in (
                ("max_min_fair", None, GroupedStorage),
                ("max_min_fair", "per_block", Storage),
                ("active_split", None, Storage)):
            cfg = config()
            cfg["bandwidth_policy"] = policy
            if representation:
                cfg["max_min_read_representation"] = representation
            self.assertIs(type(Service(dict(block_size=1, storage=cfg), []).store), expected)
        cfg["max_min_read_representation"] = "typo"
        with self.assertRaises(ValueError):
            Service(dict(block_size=1, storage=cfg), [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(GroupedStorageTest))
    if args.output:
        files = [Path(__file__), ROOT / "sim/grouped_storage.py", ROOT / "sim/storage.py",
                 ROOT / "sim/stack/storage_service.py"]
        for source in files:
            target = args.output / "runtime_sources" / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        (args.output / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in EVIDENCE))
        (args.output / "report.json").write_text(json.dumps(dict(
            passed=result.wasSuccessful(), tests=result.testsRun, scenarios=len(EVIDENCE),
            clock_roundoff=PRECISION_EVIDENCE,
            old_stalled_event_count=sum(len(row["old_stalled_events"]) for row in EVIDENCE),
            checked_states=sum(len(row["events"]) for row in EVIDENCE),
            numerical_budgets={key: dict(abs_tol=a, rel_tol=r) for key, (a, r) in BUDGETS.items()},
            max_absolute_errors={key: max((row["max_absolute_errors"][key] for row in EVIDENCE), default=0.)
                                 for key in BUDGETS},
            reference=str(REFERENCE.relative_to(ROOT)),
            reference_sha256=json.loads((REFERENCE / "manifest.json").read_text())["source_sha256"],
            source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            boundary="Numeric model-time equivalence, not bitwise identity. Event predictions within "
                     "the time budget are coalesced to the later prediction in this test only."
        ), indent=2) + "\n")
    sys.exit(not result.wasSuccessful())
