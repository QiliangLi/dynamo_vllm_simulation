"""cap 数值来源策略（legacy/measured/fixed）与弹性超发的不变量；不加载 vLLM/Dynamo。"""

import json
import math
from pathlib import Path
import unittest

from sim.admission import AdmissionController
from sim.request_profile import normalize_config
from sim.stack.read_admission import FrozenAdmission
from sim.control_events import ControlEvent
from sim.request_profile import RequestProfile


def profile(rid, pressure, arrival=0, service=1):
    return RequestProfile(rid, arrival, "evaluation-only", 100, 16 if pressure else 0,
                          int(pressure), 1, pressure, service, arrival + 4 * service)


def controller(**kwargs):
    c = {"mode": "tfs", "quantile": .75, "reference_bandwidth_Bps": 10,
         "release_on": "first_token", "aging_enabled": True,
         "aging_wait_s": 1, "aging_cooldown_s": .5, "feedback_delay_s": 0}
    c.update(kwargs)
    return AdmissionController(c)


def submit(a, now=0):
    d = a.pick(now)
    a.reserve(d, now)
    a.mark_submitted(d.request_id, 0, now)
    return d.request_id


def event(kind, rid, occurred=1, visible=1, eid=None, **payload):
    return ControlEvent(eid or f"{kind}:{rid}", kind, rid, 0, occurred, visible, payload)


def base_cfg():
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root / "configs/demo.json").read_text())
    c["admission"] = dict(mode="tfs", reference_bandwidth_Bps=1,
                          release_on="first_token", feedback_delay_s=0, aging_enabled=False)
    return c


class FixedCapTests(unittest.TestCase):
    def test_fixed_cap_ignores_theta(self):
        a = controller(cap_policy="fixed", cap_fixed_int=5)
        a.on_arrivals([profile(str(i), d) for i, d in enumerate([0, 1, 10, 20])], 0)
        self.assertEqual((a.theta, a.cap), (10, 5))


class MeasuredCapTests(unittest.TestCase):
    def test_fallback_to_theta_below_min_samples(self):
        a = controller(cap_policy="measured", cap_measured_min_samples=2)
        a.on_arrivals([profile(str(i), 20, service=.1) for i in range(4)], 0)
        self.assertEqual((a.theta, a.cap), (20, 1))
        basis = [e for e in a.logs if e["event"] == "threshold_updated"][-1]["cap_basis"]
        self.assertEqual(basis, "measured_fallback_theta")

    def _finish_two_heavies(self, a):
        rid0 = submit(a, 0)
        a.on_event(event("first_token", rid0, occurred=10, visible=10), 10)
        rid1 = submit(a, 10)
        a.on_event(event("first_token", rid1, occurred=20, visible=20), 20)
        return rid0, rid1

    def test_measured_median_replaces_theta(self):
        a = controller(cap_policy="measured", cap_measured_min_samples=2)
        a.on_arrivals([profile(str(i), 20, service=.1) for i in range(4)], 0)
        self._finish_two_heavies(a)
        # 每条样本 rate=20 bytes/10 s=2 B/s → cap=floor(10/2)=5
        self.assertEqual(a.heavy_rate_samples, [2.0, 2.0])
        a.on_arrivals([profile("new", 20, arrival=20)], 20)
        self.assertEqual(a.cap, 5)
        entry = [e for e in a.logs if e["event"] == "threshold_updated"][-1]
        self.assertEqual(entry["cap_basis"], "measured")
        self.assertEqual(entry["measured_samples"], 2)

    def test_light_requests_are_not_sampled(self):
        a = controller(cap_policy="measured", cap_measured_min_samples=1)
        a.on_arrivals([profile("h", 20, service=.1), profile("l", 1, service=.01),
                       profile("h2", 20, service=.1), profile("h3", 20, service=.1)], 0)
        # light 的 EDF slack 最小（deadline-service 更早），pick 必先取 light
        rid = submit(a, 0)
        self.assertEqual(rid, "l")
        self.assertFalse(a.is_heavy(a.records[rid].profile))
        a.on_event(event("first_token", rid, occurred=5, visible=5), 5)
        self.assertEqual(a.heavy_rate_samples, [])

    def test_zero_window_not_sampled(self):
        a = controller(cap_policy="measured", cap_measured_min_samples=1)
        a.on_arrivals([profile(str(i), 20, service=.1) for i in range(4)], 0)
        rid = submit(a, 0)
        a.on_event(event("first_token", rid, occurred=0, visible=0), 0)  # 窗口为 0
        self.assertEqual(a.heavy_rate_samples, [])


def frozen(**kwargs):
    c = {"mode": "tfs", "quantile": .75, "reference_bandwidth_Bps": 10,
         "release_on": "first_token", "aging_enabled": False,
         "placement": "scheduler", "classification": "arrival"}
    c.update(kwargs)
    return FrozenAdmission(c)


class OverdraftTests(unittest.TestCase):
    def setUp(self):
        self.a = frozen(overdraft_wait_s=1.0, overdraft_max_extra=1)
        self.a.on_arrivals([profile(h, 20, service=.1) for h in ("h0", "h1", "h2", "h3")], 0)
        # theta=20 → legacy cap=1
        for rid in ("h0", "h1", "h2"):
            self.a.dispatch_unreserved(rid, 0)

    def test_overdraft_grants_after_wait_threshold(self):
        self.assertTrue(self.a.acquire("h0", 0, "k0", 1, self.a.hints["h0"], 0.0)["granted"])
        # held=1=cap：未到等待阈值 → 拒绝
        r = self.a.acquire("h1", 1, "k1", 1, self.a.hints["h1"], 0.5)
        self.assertFalse(r["granted"])
        self.assertEqual(r["reason"], "full")
        # 等待满 1s → 超发一条（held 1 < cap+extra 2）
        r = self.a.acquire("h1", 1, "k1", 1, self.a.hints["h1"], 1.0)
        self.assertTrue(r["granted"])
        events = [e for e in self.a.logs if e["event"] == "read_permit_overdraft"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["waited_s"], 1.0)
        # permit 标记与日志 reason
        permit = self.a.permits[r["permit_id"]]
        self.assertTrue(permit["overdraft"])
        reserved = [e for e in self.a.logs if e["event"] == "admission_reserved"][-1]
        self.assertEqual(reserved["reason"], "overdraft")

    def test_overdraft_ceiling_is_cap_plus_extra(self):
        self.a.acquire("h0", 0, "k0", 1, self.a.hints["h0"], 0.0)
        self.a.acquire("h1", 1, "k1", 1, self.a.hints["h1"], 1.0)
        # held=2=cap+extra：继续拒绝
        r = self.a.acquire("h2", 2, "k2", 1, self.a.hints["h2"], 2.0)
        self.assertFalse(r["granted"])

    def test_disabled_by_default_threshold_zero(self):
        a = frozen()  # overdraft_wait_s 默认 0 → 关闭
        a.on_arrivals([profile(h, 20, service=.1) for h in ("h0", "h1", "h2", "h3")], 0)
        for rid in ("h0", "h1"):
            a.dispatch_unreserved(rid, 0)
        a.acquire("h0", 0, "k0", 1, a.hints["h0"], 0.0)
        r = a.acquire("h1", 1, "k1", 1, a.hints["h1"], 10.0)
        self.assertFalse(r["granted"])


class NormalizeCapPolicyTests(unittest.TestCase):
    def test_defaults_keep_legacy(self):
        a = normalize_config(base_cfg())["admission"]
        self.assertEqual(a["cap_policy"], "legacy")
        self.assertEqual(a["overdraft_wait_s"], 0.0)

    def test_invalid_policy_rejected(self):
        c = base_cfg(); c["admission"]["cap_policy"] = "bogus"
        with self.assertRaises(ValueError):
            normalize_config(c)

    def test_fixed_requires_positive_int(self):
        c = base_cfg(); c["admission"]["cap_policy"] = "fixed"
        with self.assertRaises(ValueError):
            normalize_config(c)
        c["admission"]["cap_fixed_int"] = 3
        normalize_config(c)
        c["admission"]["cap_fixed_int"] = 0
        with self.assertRaises(ValueError):
            normalize_config(c)

    def test_negative_overdraft_rejected(self):
        c = base_cfg(); c["admission"]["overdraft_wait_s"] = -1
        with self.assertRaises(ValueError):
            normalize_config(c)


if __name__ == "__main__":
    unittest.main()
