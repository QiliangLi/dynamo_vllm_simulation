"""Focused checks for the bandwidth-permit scheduler overlay.

Run against a harness after applying bandwidth_central.patch and
bandwidth_scheduler.patch, with the pinned vLLM source on PYTHONPATH.
"""
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from sim.engine import Engine, make_request
from sim.stack.scheduler_admission import ReadGate, patch_schedule


class BandwidthSchedulerTests(unittest.TestCase):
    def setUp(self):
        import sim.engine
        root = Path(sim.engine.__file__).resolve().parents[1]
        cfg = json.loads((root / "configs/demo.json").read_text())
        cfg.update(scheduler="upstream", enable_prefix_caching=False, max_num_seqs=1)
        self.engine = Engine(0, cfg, SimpleNamespace(present=set()))
        self.scheduler = self.engine.scheduler
        self.client = Mock()
        self.gate = ReadGate(self.client, 0, bandwidth_share_Bps=6.25e9)
        self.scheduler._read_gate = self.gate
        self.scheduler.schedule = MethodType(
            patch_schedule(type(self.scheduler).schedule), self.scheduler)

    def request(self, rid, pressure, *, heavy=False, arrival=0):
        req = make_request(dict(id=rid, prompt_token_ids=[100]*64,
                                output_tokens=1, arrival_s=arrival), 16, "fcfs")
        req.sampling_params.extra_args = dict(storage_hint=dict(
            version=1, is_heavy_read=heavy, threshold_version=1,
            estimated_remote_read_bytes=32*131072, pressure_Bps=pressure*1e9))
        return req

    def add(self, *args, **kwargs):
        req = self.request(*args, **kwargs)
        self.scheduler.add_request(req)
        return req

    def refuse(self, remaining=10):
        self.client.control.return_value = dict(granted=False, reason="full",
                                                remaining_Bps=remaining*1e9)

    def test_denied_heavy_selects_largest_fitting_light_across_queues(self):
        self.add("heavy", 15, heavy=True)
        self.add("zero", 0)
        self.add("small", 3)
        self.add("six-later", 6, arrival=2)
        self.add("six-older", 6, arrival=1)
        self.add("eight", 8)
        self.refuse()
        output = self.scheduler.schedule()
        self.assertEqual(set(output.num_scheduled_tokens), {"six-older"})
        self.assertEqual(self.gate.light_limit_Bps, 6.25e9)
        self.assertEqual([r.request_id for r in self.scheduler.waiting],
                         ["zero", "small", "six-later", "eight"])
        self.assertEqual([r.request_id for r in self.scheduler.skipped_waiting], ["heavy"])
        self.assertEqual(self.client.control.call_count, 1)

    def test_without_new_acquire_keeps_previous_refusal(self):
        self.refuse(4)
        self.assertFalse(self.gate.allow(self.request("old", 20, heavy=True)))
        self.client.control.reset_mock()
        self.add("zero", 0)
        self.add("three", 3)
        self.add("six", 6)
        output = self.scheduler.schedule()
        self.assertEqual(set(output.num_scheduled_tokens), {"three"})
        self.client.control.assert_not_called()

    def test_no_fitting_light_selects_minimum(self):
        self.add("heavy", 15, heavy=True)
        self.add("eight", 8)
        self.add("seven", 7)
        self.refuse()
        self.assertEqual(set(self.scheduler.schedule().num_scheduled_tokens), {"seven"})

    def test_smaller_heavy_can_acquire_after_larger_refusal(self):
        self.add("large", 15, heavy=True)
        self.add("larger", 20, heavy=True)
        self.add("small", 10, heavy=True)
        # No remote KV in this fixture, so the native path rolls back the grant.
        self.client.control.side_effect = [
            dict(granted=False, reason="full", remaining_Bps=10e9),
            dict(granted=True, permit_id="small-permit"),
            dict(released=True),
        ]
        output = self.scheduler.schedule()
        self.assertEqual(set(output.num_scheduled_tokens), {"small"})
        acquisitions = [c.args[1] for c in self.client.control.call_args_list
                        if c.args[0] == "acquire"]
        self.assertEqual(acquisitions, ["large", "small"])
        self.assertIsNone(self.gate.light_limit_Bps)
        self.assertFalse(self.gate.full)

    def test_native_order_until_first_refusal(self):
        self.add("oldest", 0)
        self.add("six", 6)
        self.assertEqual(set(self.scheduler.schedule().num_scheduled_tokens), {"oldest"})
        self.client.control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
