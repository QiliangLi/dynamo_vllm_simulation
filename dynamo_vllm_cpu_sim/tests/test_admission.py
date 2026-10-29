"""配额、可见时间、重分类与老化的不变量；不加载 vLLM/Dynamo。"""

from dataclasses import replace
import unittest

from sim.admission import AdmissionController
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


class AdmissionTests(unittest.TestCase):
    def test_quantile_and_same_time_batch(self):
        a = controller()
        a.on_arrivals([profile(str(i), d) for i, d in enumerate([0, 1, 10, 20])], 0)
        self.assertEqual((a.theta, a.cap, a.threshold_version), (10, 1, 1))

    def test_zero_quantile_and_all_zero(self):
        a = controller()
        a.on_arrivals([profile(str(i), 0) for i in range(5)], 0)
        self.assertIsNone(a.cap)
        a.on_arrivals([profile("positive", 20)], 0)
        self.assertEqual(a.theta, 20)
        self.assertFalse(a.is_heavy(a.records["0"].profile))

    def test_atomic_reservation_and_skip_heavy(self):
        a = controller()
        a.on_arrivals([profile("heavy1", 20, service=.1), profile("heavy2", 20, service=.1),
                       profile("light", 1, service=1)], 0)
        d = a.pick(0)
        a.reserve(d, 0)
        with self.assertRaises(ValueError):
            a.reserve(d, 0)
        self.assertEqual(a.pick(0).request_id, "light")
        self.assertEqual(a.held, 1)

    def test_release_modes_and_idempotence(self):
        for release_on in ("first_token", "load_done"):
            a = controller(release_on=release_on)
            a.on_arrivals([profile("r", 20)], 0)
            submit(a)
            a.on_event(event("load_submitted", "r", .1, .1, attempt_id=1, submitted_read_bytes=100), .1)
            done = event("load_completed", "r", attempt_id=1)
            a.on_event(done, 1)
            self.assertEqual(a.held, int(release_on == "first_token"))
            a.on_event(done, 1)
            a.on_event(event("first_token", "r", 2, 2), 2)
            a.on_event(event("request_finished", "r", 3, 3), 3)
            self.assertEqual(a.held, 0)
            self.assertEqual(sum(x["event"] == "admission_released" for x in a.logs), 1)

    def test_delayed_feedback_is_not_future_information(self):
        a = controller(release_on="load_done")
        a.on_arrivals([profile("r", 20)], 0)
        submit(a)
        a.on_event(event("load_submitted", "r", .1, .1, attempt_id=1, submitted_read_bytes=100), .1)
        e = event("load_completed", "r", 1, 2, attempt_id=1)
        with self.assertRaises(ValueError):
            a.on_event(e, 1.5)
        self.assertEqual(a.held, 1)
        a.on_event(e, 2)
        self.assertEqual(a.held, 0)

    def test_no_external_load_release(self):
        for mode in ("first_token", "load_done"):
            a = controller(release_on=mode)
            a.on_arrivals([profile("r", 20)], 0)
            submit(a)
            a.on_event(event("no_external_load", "r"), 1)
            self.assertEqual(a.held, int(mode == "first_token"))

    def test_reclassification_never_reopens_closed_lease(self):
        a = controller()
        a.on_arrivals([profile("lo", 1, service=.5), profile("hi", 20)], 0)
        self.assertEqual(a.theta, 1)
        submit(a)
        a.on_event(event("first_token", "lo"), 1)
        a.on_arrivals([profile(f"new{i}", 100) for i in range(8)], 1)
        self.assertFalse(a.records["lo"].lease_open)
        self.assertEqual(len(a.records), 10)
        # 在另一控制器中，让已放行轻请求变重；动态 held 可以超过新 cap。
        b = controller()
        b.on_arrivals([profile(f"light{i}", 10, service=.1) for i in range(3)]
                      + [profile(str(i), 100) for i in range(9)], 0)
        for i in range(3):
            self.assertEqual(submit(b), f"light{i}")
        for i in range(40):
            b.on_arrivals([profile(f"zero{i}", 0)], 0)
        self.assertTrue(b.is_heavy(b.records["light0"].profile))
        self.assertEqual(b.held, 3)
        self.assertEqual(b.cap, 1)
        self.assertTrue(all(b.records[f"light{i}"].lease_open for i in range(3)))

    def test_aging_cannot_burst_and_timer_advances(self):
        a = controller()
        a.on_arrivals([profile(str(i), 20) for i in range(4)], 0)
        submit(a)
        self.assertIsNone(a.pick(.9))
        self.assertEqual(a.next_wakeup(.9), 1)
        self.assertEqual(a.pick(1).reason, "aging")
        submit(a, 1)
        self.assertIsNone(a.pick(1))
        self.assertEqual(a.next_wakeup(1), 1.5)
        self.assertEqual(a.held, 2)
        self.assertEqual(a.exemptions, 1)

    def test_prefix_equivalence_and_no_class_decisions(self):
        a, b = controller(), controller()
        xs = [profile("a", 20), profile("b", 1)]
        a.on_arrivals(xs, 0)
        b.on_arrivals([replace(p, class_id="changed") for p in xs], 0)
        for _ in xs:
            self.assertEqual(submit(a), submit(b))
        # 后缀尚未到达时，决策历史完全相同。
        self.assertEqual(a.snapshot(0), b.snapshot(0))
        with self.assertRaises(ValueError):
            b.on_arrivals([profile("future", 100, arrival=5)], 1)

    def test_repeated_load_marks_out_of_scope(self):
        a = controller()
        a.on_arrivals([profile("r", 20)], 0)
        submit(a)
        for attempt in (1, 2):
            a.on_event(event("load_submitted", "r", eid=str(attempt), attempt_id=attempt,
                             submitted_read_bytes=100), 1)
        self.assertIn("repeated_external_load", a.invalid_reasons)
