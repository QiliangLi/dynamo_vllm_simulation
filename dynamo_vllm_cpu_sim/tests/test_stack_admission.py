"""跨进程准入适配的事实边界；策略本体仍由已有 admission 测试覆盖。"""
import asyncio
import json
from pathlib import Path
import time
import unittest

from sim.stack.admission_service import AdmissionState


def config(release="first_token",delay=0):
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root/"configs/demo.json").read_text())
    c["admission"] = dict(mode="tfs",reference_bandwidth_Bps=1,
                          release_on=release,feedback_delay_s=delay,aging_enabled=False)
    return c


class StackAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def arrive(self,s,rid):
        return s.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))

    def raw(self,s,rid,kind,event="e1",payload=None,worker=1):
        return dict(request_id=s.uuid_by_id[rid]+"_0",event_id=event,kind=kind,
                    worker_id=worker,occurred_mono=time.monotonic(),payload=payload or {})

    async def test_quota_holds_until_real_first_token(self):
        s = AdmissionState(config())
        a,b = self.arrive(s,"a"),self.arrive(s,"b")
        s.admit()
        self.assertTrue(a.done())
        self.assertFalse(b.done())
        s.receive(self.raw(s,"a","load_submitted",payload=dict(attempt_id=1,submitted_read_bytes=1024)))
        s.receive(self.raw(s,"a","load_completed","e2",dict(attempt_id=1)))
        s.admit()
        self.assertFalse(b.done())
        s.local_event("first_token","a")
        s.admit()
        self.assertTrue(b.done())
        self.assertEqual(s.controller.records["a"].worker_id,1)

    async def test_load_done_and_no_load_release_once(self):
        s = AdmissionState(config("load_done"))
        self.arrive(s,"a")
        self.arrive(s,"b")
        s.admit()
        event = self.raw(s,"a","no_external_load")
        s.receive(event)
        s.receive(event)
        s.admit()
        self.assertTrue(s.futures["b"].done())
        self.assertEqual(sum(e["event"]=="admission_released" for e in s.events),1)

    async def test_feedback_delay_is_respected(self):
        s = AdmissionState(config("load_done",delay=0.02))
        self.arrive(s,"a")
        self.arrive(s,"b")
        s.admit()
        s.receive(self.raw(s,"a","no_external_load"))
        s.admit()
        self.assertFalse(s.futures["b"].done())
        await asyncio.sleep(0.03)
        s.admit()
        self.assertTrue(s.futures["b"].done())

    async def test_wrong_worker_or_unregistered_id_fails(self):
        s = AdmissionState(config())
        self.arrive(s,"a")
        s.admit()
        s.receive(self.raw(s,"a","no_external_load"))
        with self.assertRaises(ValueError):
            s.receive(self.raw(s,"a","no_external_load","e2",worker=2))
        raw = self.raw(s,"a","no_external_load")
        raw["request_id"] = "unregistered"
        with self.assertRaises(ValueError):
            s.receive(raw)

    async def test_uncertain_upstream_never_admits_next(self):
        s = AdmissionState(config())
        self.arrive(s,"a")
        self.arrive(s,"b")
        s.admit()
        s.fatal = "connection lost while upstream may still be running"
        s.admit()
        self.assertFalse(s.futures["b"].done())
        self.assertTrue(s.controller.records["a"].lease_open)

    async def test_completion_without_submit_fails(self):
        s = AdmissionState(config("load_done"))
        self.arrive(s,"a")
        s.admit()
        with self.assertRaises(ValueError):
            s.receive(self.raw(s,"a","load_completed",payload=dict(attempt_id=1)))

    async def test_only_queued_cancellation_removes_request(self):
        s = AdmissionState(config())
        self.arrive(s,"a")
        pending = self.arrive(s,"b")
        s.admit()
        self.assertFalse(s.cancel_queued("a"))
        self.assertTrue(s.controller.records["a"].lease_open)
        self.assertTrue(s.cancel_queued("b"))
        self.assertTrue(pending.cancelled())
        self.assertNotIn("b",s.controller.pending)
        self.assertFalse(s.controller.records["b"].lease_open)


if __name__ == "__main__":
    unittest.main()
