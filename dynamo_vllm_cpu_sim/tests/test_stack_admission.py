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
        s.admit()
        self.assertFalse(b.done())
        s.local_event("first_token","a")
        s.admit()
        self.assertTrue(b.done())
        self.assertEqual(s.controller.records["a"].worker_id,-1)

    async def test_repeated_token_releases_once(self):
        s = AdmissionState(config())
        self.arrive(s,"a")
        self.arrive(s,"b")
        s.admit()
        s.local_event('first_token','a')
        s.local_event('first_token','a')
        s.admit()
        self.assertTrue(s.futures["b"].done())
        self.assertEqual(sum(e["event"]=="admission_released" for e in s.events),1)

    async def test_feedback_delay_is_respected(self):
        s = AdmissionState(config(delay=0.02))
        self.arrive(s,"a")
        self.arrive(s,"b")
        s.admit()
        s.local_event('first_token','a')
        s.admit()
        self.assertFalse(s.futures["b"].done())
        await asyncio.sleep(0.03)
        s.admit()
        self.assertTrue(s.futures["b"].done())

    async def test_execution_feedback_is_rejected(self):
        s = AdmissionState(config())
        self.arrive(s,"a")
        s.admit()
        for kind in ('load_submitted','layer_load_submitted','load_completed','no_external_load'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'removed'):
                s.receive(self.raw(s,'a',kind))
        self.assertTrue(s.controller.records['a'].lease_open)

    async def test_uncertain_upstream_never_admits_next(self):
        s = AdmissionState(config())
        self.arrive(s,"a")
        self.arrive(s,"b")
        s.admit()
        s.fatal = "connection lost while upstream may still be running"
        s.admit()
        self.assertFalse(s.futures["b"].done())
        self.assertTrue(s.controller.records["a"].lease_open)

    async def test_load_done_release_is_not_silently_kept(self):
        with self.assertRaisesRegex(ValueError, 'first_token'):
            AdmissionState(config('load_done'))

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
