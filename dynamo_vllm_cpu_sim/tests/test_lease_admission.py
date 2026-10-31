"""FrozenLeaseAdmission 生命周期：hint v2、租约申请、事件释放语义与幂等。"""
import json
from pathlib import Path
import time
import unittest

from sim.stack.admission_rpc import clock_id
from sim.stack.admission_service import AdmissionState
from sim.stack.storage_hint import attach_http_hint, hint_from_request, validate_hint


def config():
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root / "configs/demo.json").read_text())
    c["scheduler"] = "upstream"
    c["storage"].update(disks=1, disk_Bps=30e9, shared_link_Bps=30e9, read_latency_s=0.0)
    c["admission"] = dict(mode="tfs", reference_bandwidth_Bps=30e9, release_on="lease_events",
                          aging_enabled=False, placement="scheduler",
                          classification="bandwidth_line")
    return c


def v2_hint(heavy=True, demand=20e9, blocks=1250, read_bytes=1_600_000_000):
    return dict(version=2, is_heavy_read=heavy,
                estimated_remote_read_bytes=read_bytes, threshold_version=1,
                demand_Bps=demand, remote_blocks=blocks,
                per_disk_shares=[{"disk": 0, "share": 1.0}])


class HintV2Tests(unittest.TestCase):
    def test_validate_roundtrip(self):
        hint = v2_hint()
        self.assertEqual(validate_hint(hint), hint)
        body = attach_http_hint(dict(prompt="x"), hint)
        self.assertEqual(hint_from_request(body["nvext"]), hint)

    def test_v2_rejects_bad_shapes(self):
        bad = v2_hint()
        bad["per_disk_shares"] = [{"disk": 0, "share": 0.5}]
        with self.assertRaises(ValueError):
            validate_hint(bad)
        bad = v2_hint(demand=-1.0)
        with self.assertRaises(ValueError):
            validate_hint(bad)
        bad = v2_hint()
        bad.pop("remote_blocks")
        with self.assertRaises(ValueError):
            validate_hint(bad)
        bad = v2_hint()
        bad["version"] = 3
        with self.assertRaises(ValueError):
            validate_hint(bad)

    def test_v1_still_valid(self):
        hint = dict(version=1, is_heavy_read=True, estimated_remote_read_bytes=10,
                    threshold_version=2)
        self.assertEqual(validate_hint(hint), hint)


class LeaseAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = AdmissionState(config())

    def arrive(self, rid, remote=32, prompt=100):
        return self.state.arrive(dict(id=rid, prompt_token_ids=list(range(prompt)),
                                      remote_prefix_tokens=remote))

    def raw(self, rid, op, **extra):
        row = dict(op=op, request_id=self.state.uuid_by_id[rid] + "-0", worker_id=1)
        row.update(extra)
        return row

    def acquire(self, rid, key="k1"):
        self.state.admit()  # scheduler 放置：立即派发
        hint = dict(self.state.controller.hints[rid])
        return self.state.control(self.raw(rid, "acquire", acquisition_key=key,
                                           load_attempt_id=1, storage_hint=hint)), hint

    async def test_hint_v2_marks_heavy_by_bandwidth_line(self):
        self.arrive("a", remote=32)
        hint = self.state.controller.hints["a"]
        profile = self.state.controller.records["a"].profile
        self.assertEqual(hint["version"], 2)
        self.assertEqual(hint["demand_Bps"], profile.pressure_Bps)
        self.assertEqual(hint["per_disk_shares"], [{"disk": 0, "share": 1.0}])
        self.assertEqual(hint["is_heavy_read"],
                         profile.pressure_Bps > 3e9)
        self.assertEqual(hint["remote_blocks"],
                         profile.estimated_remote_tokens // 16 if hint["is_heavy_read"] else 0)

    async def test_grant_start_complete_lifecycle(self):
        self.arrive("a", remote=32)
        reply, hint = self.acquire("a")
        self.assertTrue(reply["granted"])
        self.assertIn("ttl_s", reply)
        token = reply["permit_id"]
        self.assertGreater(self.state.controller.ledger.heavy_sum(), 0)
        started = self.state.control(self.raw("a", "lease_started", permit_id=token,
                                              occurred_mono=time.monotonic(), clock_id=clock_id()))
        self.assertTrue(started["started"])
        permit = self.state.controller.permits[token]
        self.assertEqual(permit["state"], "submitted")
        # first_token 不释放租约。
        self.state.local_event("first_token", "a")
        self.state.deliver()
        self.assertTrue(self.state.controller.records["a"].lease_open)
        completed = self.state.control(self.raw("a", "lease_completed", permit_id=token,
                                                occurred_mono=time.monotonic(), clock_id=clock_id()))
        self.assertTrue(completed["released"])
        self.assertFalse(self.state.controller.records["a"].lease_open)
        self.assertEqual(self.state.controller.records["a"].lease_release_reason,
                         "lease_completed")
        self.assertEqual(self.state.controller.ledger.heavy_sum(), 0.0)

    async def test_acquire_replay_and_bad_echo_hint(self):
        self.arrive("a", remote=32)
        reply, hint = self.acquire("a", key="k1")
        replay = self.state.control(self.raw("a", "acquire", acquisition_key="k1",
                                             load_attempt_id=1, storage_hint=hint))
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["permit_id"], reply["permit_id"])
        forged = dict(hint, is_heavy_read=False)
        with self.assertRaises(ValueError):
            self.state.control(self.raw("a", "acquire", acquisition_key="k2",
                                        load_attempt_id=1, storage_hint=forged))

    async def test_lease_events_reject_wrong_owner(self):
        self.arrive("a", remote=32)
        reply, _ = self.acquire("a")
        raw = self.raw("a", "lease_started", permit_id=reply["permit_id"],
                       occurred_mono=time.monotonic(), clock_id=clock_id())
        raw["worker_id"] = 0
        with self.assertRaises(ValueError):
            self.state.control(raw)

    async def test_rollback_and_request_finished_fallback(self):
        self.arrive("a", remote=32)
        reply, _ = self.acquire("a")
        self.state.control(self.raw("a", "rollback", permit_id=reply["permit_id"]))
        self.assertFalse(self.state.controller.records["a"].lease_open)
        self.assertEqual(self.state.controller.records["a"].lease_release_reason,
                         "allocation_failed")
        # 二次回滚幂等。
        self.state.control(self.raw("a", "rollback", permit_id=reply["permit_id"]))

        self.arrive("b", remote=32)
        reply_b, _ = self.acquire("b")
        self.state.control(self.raw("b", "lease_started", permit_id=reply_b["permit_id"],
                                    occurred_mono=time.monotonic(), clock_id=clock_id()))
        self.state.local_event("first_token", "b")
        self.state.local_event("request_finished", "b")
        self.state.deliver()
        self.assertFalse(self.state.controller.records["b"].lease_open)
        self.assertEqual(self.state.controller.records["b"].lease_release_reason,
                         "request_finished")
        self.assertEqual(self.state.controller.ledger.heavy_sum(), 0.0)

    async def test_late_lease_events_are_tolerated(self):
        # TTL/完成兜底先释放后，迟到的读毕/读开始事件只记录，不杀引擎、不重复减账。
        self.arrive("a", remote=32)
        reply, _ = self.acquire("a")
        token = reply["permit_id"]
        self.state.control(self.raw("a", "lease_started", permit_id=token,
                                    occurred_mono=time.monotonic(), clock_id=clock_id()))
        self.state.local_event("first_token", "a")
        self.state.local_event("request_finished", "a")
        self.state.deliver()
        self.assertFalse(self.state.controller.records["a"].lease_open)
        late = self.state.control(self.raw("a", "lease_completed", permit_id=token,
                                           occurred_mono=time.monotonic(), clock_id=clock_id()))
        self.assertTrue(late["completed"] and late.get("late"))
        self.assertEqual(self.state.controller.ledger.heavy_sum(), 0.0)
        raw = dict(op="lease_started", request_id=self.state.uuid_by_id["a"] + "-1",
                   worker_id=1, permit_id=token,
                   occurred_mono=time.monotonic(), clock_id=clock_id())
        self.state.controller.lease_events_seen.discard(f"{token}:started")
        late_start = self.state.control(raw)
        self.assertTrue(late_start["started"] and late_start.get("late"))

    async def test_warmup_bypass_grants_without_line(self):
        self.arrive("a", remote=32)
        self.state.controller.ledger.warmup = True
        reply, _ = self.acquire("a")
        self.assertTrue(reply["granted"])
        self.assertEqual(reply["path"], "normal")

    async def test_snapshot_exposes_lease_state(self):
        self.arrive("a", remote=32)
        self.acquire("a")
        snap = self.state.snapshot()
        self.assertEqual(snap["execution_feedback"], "lease_events")
        self.assertEqual(snap["lease"]["counters"]["normal"], 1)
        self.assertEqual(len(snap["lease"]["open"]), 1)


if __name__ == "__main__":
    unittest.main()
