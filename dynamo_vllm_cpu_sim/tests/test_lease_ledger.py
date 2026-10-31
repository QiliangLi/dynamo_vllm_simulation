"""租约账本五步放行、护栏、waterfill 同构与兜底的单元测试（30 GB/s 向量对齐方案 §4）。"""
import math
import unittest

from sim.grouped_storage import weighted_waterfill
from sim.stack.lease_ledger import LeaseEntry, LeaseLedger

GB = 1e9


def lease_config(**over):
    cfg = dict(line_factor_heavy=0.1, line_factor_normal=1.1, line_factor_exception=1.5,
               compute_slots=4, loading_slots_per_worker=4, ttl_slack_factor=1.5,
               ttl_margin_s=0.05, reconcile_interval_s=0.05, reconcile_tolerance=0.1,
               disks=1)
    cfg.update(over)
    return cfg


def entry(rid, share, blocks, read_bytes, compute_s, service_s=None, deadline=1e9,
          worker=0, granted=0.0):
    return LeaseEntry(rid, worker, None, 0, share, blocks, read_bytes, compute_s,
                      service_s if service_s is not None else compute_s, deadline,
                      granted, math.inf)


class FiveStepTests(unittest.TestCase):
    def setUp(self):
        self.led = LeaseLedger(lease_config(), 30 * GB, 1)
        self.assertAlmostEqual(self.led.line_Bps / GB, 33.0, places=6)
        self.assertAlmostEqual(self.led.exception_Bps / GB, 45.0, places=6)

    def test_normal_path_below_line(self):
        d = self.led.evaluate(entry("c1", 20 * GB, 20, 600e6, 0.03), [], 0.0)
        self.assertTrue(d["granted"])
        self.assertEqual(d["path"], "normal")

    def test_exception_path_walkthrough(self):
        # 方案 §4 走查：账本 21，候选 20 → 41 ≤ 45，条件过 → 例外放行。
        self.led.admit(entry("R3", 21 * GB, 21, 630e6, 0.03, deadline=100.0))
        d = self.led.evaluate(entry("R4", 20 * GB, 20, 600e6, 0.03, deadline=100.0), [], 50.0)
        self.assertTrue(d["granted"])
        self.assertEqual(d["path"], "exception")
        self.assertAlmostEqual(d["k"], 41 / 30, places=9)

    def test_exception_band_denial(self):
        # 账本 41（R3+R4 例外放行后），候选 20 → 61 > 45 → 界外拒。
        self.led.admit(entry("R3", 21 * GB, 21, 630e6, 0.03, deadline=100.0))
        self.led.admit(entry("R4", 20 * GB, 20, 600e6, 0.03, deadline=100.0))
        d = self.led.evaluate(entry("R5", 20 * GB, 20, 600e6, 0.03, deadline=100.0), [], 50.0)
        self.assertFalse(d["granted"])
        self.assertEqual(d["reason"], "full")
        self.assertEqual(self.led.counters["denied_exception_band"], 1)

    def test_guardrail_empty_ledger_must_grant(self):
        # 单条需求即可越过例外带时，空账护栏仍放一条。
        d = self.led.evaluate(entry("huge", 50 * GB, 50, 1.5e9, 0.03), [], 0.0)
        self.assertTrue(d["granted"])
        self.assertEqual(d["path"], "guardrail_empty")

    def test_condition_a_protects_inflight_deadline(self):
        # R3 独占时可按期完成（solo≈0.021读+0.03算=0.051≤0.055），加候选被
        # 拉伸后超期 → 拒候选。
        self.led.admit(entry("R3", 21 * GB, 21, 630e6, 0.03, deadline=0.055))
        d = self.led.evaluate(entry("R4", 20 * GB, 20, 600e6, 0.03, deadline=100.0), [], 0.0)
        self.assertFalse(d["granted"])
        self.assertEqual(self.led.counters["denied_condition_a"], 1)

    def test_condition_a_ignores_already_doomed_inflight(self):
        # R3 即使不加候选也注定超期（solo≈0.051>0.04）：不再否决他人。
        self.led.admit(entry("R3", 21 * GB, 21, 630e6, 0.03, deadline=0.04))
        d = self.led.evaluate(entry("R4", 20 * GB, 20, 600e6, 0.03, deadline=100.0), [], 0.0)
        self.assertTrue(d["granted"])
        self.assertEqual(d["path"], "exception")

    def test_condition_b_protects_urgent_queue(self):
        self.led.admit(entry("R3", 21 * GB, 21, 630e6, 0.03, deadline=100.0))
        urgents = [dict(rid="R5", share_Bps=20 * GB, deadline_s=0.05, service_s=0.04)]
        d = self.led.evaluate(entry("R4", 20 * GB, 20, 600e6, 0.03, deadline=100.0),
                              urgents, 0.0)
        self.assertFalse(d["granted"])
        self.assertEqual(self.led.counters["denied_condition_b"], 1)

    def test_loading_slots_per_worker(self):
        for i in range(4):
            self.led.admit(entry(f"o{i}", 1 * GB, 1, 30e6, 0.03, worker=3))
        d = self.led.evaluate(entry("c", 20 * GB, 20, 600e6, 0.03, worker=3), [], 0.0)
        self.assertFalse(d["granted"])
        self.assertEqual(d["reason"], "worker_loading_full")
        other = self.led.evaluate(entry("c2", 20 * GB, 20, 600e6, 0.03, worker=1), [], 0.0)
        self.assertTrue(other["granted"])

    def test_light_aggregate_counts_toward_line(self):
        self.led.reconcile(30 * GB, 0.0)  # 轻读聚合抬到 ~15 GB/s
        self.led.reconcile(30 * GB, 0.0)
        self.assertGreater(self.led.light_rate_Bps, 10 * GB)
        d = self.led.evaluate(entry("c", 20 * GB, 20, 600e6, 0.03), [], 0.0)
        # 15 + 20 = 35 > 33：走例外路判据，不再是正常路。
        self.assertNotEqual(d.get("path"), "normal")


class SameArithmeticTests(unittest.TestCase):
    def test_alloc_rates_match_weighted_waterfill(self):
        led = LeaseLedger(lease_config(), 30 * GB, 1)
        entries = [entry("e1", 20 * GB, 10, 600e6, 0.03),
                   entry("e2", 10 * GB, 10, 300e6, 0.03),
                   entry("e3", 10 * GB, 5, 150e6, 0.03)]
        rates = led._alloc_rates(entries, 30 * GB)
        per_block = weighted_waterfill(30 * GB, [e.share_Bps / e.remote_blocks for e in entries],
                                       [e.remote_blocks for e in entries])
        for e, r in zip(entries, per_block):
            self.assertAlmostEqual(rates[e.rid], r * e.remote_blocks, places=6)
        self.assertAlmostEqual(sum(rates.values()), 30 * GB, places=6)

    def test_prediction_advances_with_consumed_bytes(self):
        led = LeaseLedger(lease_config(), 30 * GB, 1)
        e = entry("solo", 30 * GB, 30, 900e6, 0.03, granted=0.0)
        led.admit(e)
        rows = led.predict([e], 0.01)
        _, remaining, completion = rows[0]
        self.assertAlmostEqual(remaining, 600e6, places=-3)
        self.assertAlmostEqual(completion, 0.01 + 600e6 / (30 * GB), places=9)


class HousekeepingTests(unittest.TestCase):
    def test_ttl_sweep_reclaims_lost_completion(self):
        led = LeaseLedger(lease_config(), 30 * GB, 1)
        # 手工放置一条短 TTL 记录，模拟读毕消息丢失后的兜底回收。
        e = entry("x", 20 * GB, 20, 600e6, 0.03)
        e.expires_s = 0.01
        led.admit(e)
        self.assertEqual(led.sweep_ttl(0.009), [])
        self.assertEqual(led.sweep_ttl(0.02), ["x"])
        self.assertEqual(led.heavy_sum(), 0.0)
        self.assertEqual(led.counters["ttl_expired"], 1)

    def test_close_updates_worker_open_and_service_samples(self):
        led = LeaseLedger(lease_config(), 30 * GB, 1)
        led.admit(entry("a", 5 * GB, 5, 150e6, 0.03, worker=1, granted=0.0))
        led.admit(entry("b", 5 * GB, 5, 150e6, 0.03, worker=1, granted=0.0))
        led.close("a")
        self.assertEqual(led.worker_open[1], 1)
        led.close("b")
        self.assertNotIn(1, led.worker_open)
        self.assertEqual(led.delta_s(), 0.03)

    def test_reconcile_light_estimate_is_one_sided(self):
        led = LeaseLedger(lease_config(), 30 * GB, 1)
        led.reconcile(12 * GB, 0.0)
        self.assertAlmostEqual(led.light_rate_Bps, 6 * GB)
        led.reconcile(12 * GB, 0.05)
        self.assertAlmostEqual(led.light_rate_Bps, 9 * GB)


if __name__ == "__main__":
    unittest.main()
