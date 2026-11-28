"""Run with the snapshot overlays applied and pinned vLLM on PYTHONPATH."""
from concurrent.futures import Future
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from sim.engine import Engine, make_request
from sim.stack.admission_outbox import AdmissionOutbox
from sim.stack.scheduler_admission import ReadGate, patch_schedule


def reply(version, other):
    return dict(snapshot=dict(version=version, other_active_Bps=int(other*1e9)))


class SnapshotSchedulerTests(unittest.TestCase):
    def setUp(self):
        import sim.engine
        root = Path(sim.engine.__file__).resolve().parents[1]
        cfg = json.loads((root / "configs/demo.json").read_text())
        cfg.update(scheduler="upstream", enable_prefix_caching=False, max_num_seqs=1)
        self.engine = Engine(0, cfg, SimpleNamespace(present=set()))
        self.scheduler = self.engine.scheduler
        self.client = Mock()
        self.client.prefill_started_async.return_value = (None, None, None)
        self.gate = ReadGate(self.client, 0, bandwidth_share_Bps=6.25e9,
                             prefill_bandwidth_Bps=25e9)
        self.scheduler._read_gate = self.gate
        self.scheduler.schedule = MethodType(
            patch_schedule(type(self.scheduler).schedule), self.scheduler)

    def add(self, rid, pressure, *, heavy=False, skipped=False, hit=False):
        req = make_request(dict(id=rid, prompt_token_ids=[100]*64,
                                output_tokens=1, arrival_s=0), 16, "fcfs")
        req.sampling_params.extra_args = dict(storage_hint=dict(
            version=1, is_heavy_read=heavy, threshold_version=1,
            estimated_remote_read_bytes=32*131072, pressure_Bps=int(pressure*1e9)))
        if hit:
            self.engine.store.present.update(req.block_hashes[:2])
        self.scheduler.add_request(req)
        if skipped:
            self.scheduler.waiting.remove_request(req)
            self.scheduler.skipped_waiting.add_request(req)
        return req

    def selected(self):
        return set(self.scheduler.schedule().num_scheduled_tokens)

    def test_no_snapshot_preserves_native_skipped_queue_order(self):
        self.add("first", 20, skipped=True)
        self.add("second", 3)
        self.assertEqual(self.selected(), {"first"})
        self.client.enable_ordered_controls.assert_called_once()
        self.client.enable_idle_bypass.assert_not_called()

    def test_pick_first_fit_skipping_earlier_larger_light(self):
        self.gate.update_snapshot(reply(1, 18))  # 7 GB/s remains.
        self.add("too-large", 8, skipped=True)
        self.add("fits", 7, skipped=True)
        self.add("smaller", 3)
        self.assertEqual(self.selected(), {"fits"})

    def test_negative_budget_uses_minimum_across_queues_and_native_tie(self):
        self.gate.update_snapshot(reply(1, 30))
        self.add("zero-skipped", 0, skipped=True)
        self.add("three", 3, skipped=True)
        self.add("zero-waiting", 0)
        self.assertEqual(self.selected(), {"zero-skipped"})

    def test_all_over_budget_tries_earlier_heavy_then_reselects_from_reply(self):
        self.gate.update_snapshot(reply(1, 30))
        self.add("skipped-heavy", 20, skipped=True, heavy=True)
        self.add("newly-fitting", 7)
        self.add("minimum", 3)
        self.client.control.return_value = dict(granted=False, reason="full",
                                                remaining_Bps=8e9, **reply(2, 17))
        self.assertEqual(self.selected(), {"newly-fitting"})
        self.assertEqual(self.client.control.call_args.args, ("acquire", "skipped-heavy"))
        self.assertEqual(self.gate.prefill_snapshot["version"], 2)

    def test_all_over_budget_minimum_precedes_later_heavy(self):
        self.gate.update_snapshot(reply(1, 30))
        self.add("minimum", 3, skipped=True)
        self.add("heavy", 18, heavy=True)
        self.assertEqual(self.selected(), {"minimum"})
        self.client.control.assert_not_called()

    def test_async_reply_cannot_overwrite_newer_acquire_snapshot(self):
        self.gate.update_snapshot(reply(2, 20))
        older = Future()
        older.set_result(reply(1, 10))
        self.gate.prefill_replies.append(older)
        self.add("earlier", 8)
        self.add("fits", 5)
        self.assertEqual(self.selected(), {"fits"})

    def test_notify_once_after_confirmed_remote_allocation(self):
        req = self.add("reading", 1, hit=True)
        with patch.object(self.scheduler.kv_cache_manager, "allocate_slots", return_value=None):
            self.assertFalse(self.selected())
        self.client.prefill_started_async.assert_not_called()
        self.scheduler.schedule()
        self.client.prefill_started_async.assert_called_once_with("reading", external_load=True)
        self.gate.after_allocation(req, 32, 0)
        self.client.prefill_started_async.assert_called_once()

    def test_notification_for_small_pressure_and_no_remote_but_not_resumed_chunks(self):
        small = self.add("small", 0.99)
        cold = self.add("cold", 10)
        resumed = self.add("resumed", 10)
        resumed.num_computed_tokens = 32
        self.gate.after_allocation(small, 32, 0)
        self.gate.after_allocation(cold, 0, 0)
        self.gate.after_allocation(resumed, 32, 0)
        self.assertEqual(self.client.prefill_started_async.call_args_list, [
            call("small", external_load=True),
            call("cold", external_load=False),
        ])

    def test_non_head_allocation_failure_preserves_both_queues(self):
        self.gate.update_snapshot(reply(1, 20))
        self.add("native-first", 8, skipped=True)
        self.add("candidate", 5, hit=True)
        self.add("other", 3)
        with patch.object(self.scheduler.kv_cache_manager, "allocate_slots",
                          return_value=None) as allocate:
            self.assertFalse(self.selected())
        self.assertEqual(allocate.call_args.args[0].request_id, "candidate")
        self.assertEqual([r.request_id for r in self.scheduler.skipped_waiting], ["native-first"])
        self.assertEqual([r.request_id for r in self.scheduler.waiting], ["candidate", "other"])
        self.client.prefill_started_async.assert_not_called()

    def test_selected_non_head_with_unknown_hit_is_skipped_then_reselected(self):
        self.gate.update_snapshot(reply(1, 20))
        self.add("large", 8)
        self.add("unknown", 4)
        self.add("fallback", 3)
        original = self.scheduler.connector.get_num_new_matched_tokens

        def query(request, local_tokens):
            if request.request_id == "unknown":
                return None, False
            return original(request, local_tokens)

        with patch.object(self.scheduler.connector, "get_num_new_matched_tokens",
                          side_effect=query):
            self.assertEqual(self.selected(), {"fallback"})
        self.assertEqual([r.request_id for r in self.scheduler.waiting], ["large"])
        self.assertEqual([r.request_id for r in self.scheduler.skipped_waiting], ["unknown"])

    def test_forced_allocation_uses_one_notice_with_existing_proof(self):
        self.gate = ReadGate(self.client, 0, idle_bypass=True, prefill_bandwidth_Bps=25e9)
        self.scheduler._read_gate = self.gate
        self.client.prefill_started_async.return_value = ("forced-permit", 1.0, None)
        req = self.add("heavy", 0.5, heavy=True, hit=True)
        self.scheduler.schedule()
        call = self.client.prefill_started_async.call_args
        self.assertEqual(call.args, ("heavy",))
        self.assertEqual(call.kwargs["proof"]["external_tokens"], 32)
        self.assertTrue(call.kwargs["proof"]["allocated"])
        self.assertEqual(self.gate.permits["heavy"], "forced-permit")
        self.client.force_admit_async.assert_not_called()
        self.client.control.assert_not_called()
        self.gate.after_allocation(req, 32, 0)
        self.client.prefill_started_async.assert_called_once()


class SnapshotOutboxTests(unittest.TestCase):
    def test_plain_and_forced_start_use_distinct_fields_on_same_queue(self):
        calls = []
        def exchange(row):
            calls.append(row)
            return dict(granted=bool(row["forced"]), permit_id=row.get("permit_id"),
                        **reply(len(calls), 0))
        client = SimpleNamespace(worker=2, clock_id="same-clock", timeout_s=1,
                                 _exchange=exchange)
        outbox = AdmissionOutbox(client)
        try:
            plain_token, _, plain = outbox.prefill_started("plain", external_load=False)
            force_token, decision, forced = outbox.prefill_started(
                "forced", hint={"heavy": True}, proof={"allocated": True})
            self.assertEqual(plain.result(2), dict(granted=False, permit_id=None, **reply(1, 0)))
            self.assertEqual(forced.result(2), dict(granted=True, permit_id=force_token,
                                                   **reply(2, 0)))
            self.assertIsNone(plain_token)
            self.assertEqual(calls[0], dict(op="prefill_started", request_id="plain",
                                           worker_id=2, forced=False, external_load=False))
            self.assertEqual(calls[1]["op"], "prefill_started")
            self.assertTrue(calls[1]["forced"])
            self.assertEqual(calls[1]["occurred_mono"], decision)
            self.assertEqual(calls[1]["permit_id"], force_token)
            self.assertEqual(calls[1]["proof"], {"allocated": True})
        finally:
            outbox.close()


if __name__ == "__main__":
    unittest.main()
