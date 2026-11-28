"""Exercise the weighted permit contract against the patched simulator."""
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.run_tfs_stack import save_admission_snapshot
from sim.control_events import ControlEvent
from sim.request_profile import RequestProfile
from sim.stack.admission_service import AdmissionState
from sim.stack.read_admission import FrozenAdmission
from sim.stack.storage_hint import attach_http_hint, hint_from_request
from check_read_admission import check_bandwidth_ledger


GB = 10**9


class BandwidthCentralTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(mode="tfs", quantile=.75, release_on="first_token",
                           reference_bandwidth_Bps=25*GB, worker_count=4,
                           placement="scheduler", bandwidth_permits=True)
        self.ledger = FrozenAdmission(self.config)

    def arrive(self, rid, demand):
        p = RequestProfile(rid, 0., "test", 256, 128 if demand else 0,
                           1024 if demand else 0, .1, demand*GB, .1, .4)
        self.ledger.on_arrivals([p], 0.)
        self.ledger.dispatch_unreserved(rid, 0.)
        return self.ledger.hints[rid]

    def acquire(self, rid, key="key"):
        return self.ledger.acquire(rid, 0, key, 1, self.ledger.hints[rid], .1)

    def release(self, rid):
        self.ledger.on_event(ControlEvent(rid+"-first", "first_token", rid, 0, .2, .2), .2)

    def audit(self):
        values = check_bandwidth_ledger(
            self.ledger.logs,
            {rid: asdict(rec) for rid, rec in self.ledger.records.items()},
            self.ledger.hints, self.ledger.permits,
            dict(admission=self.config, workers=4))
        self.assertEqual(values["final_held_Bps"], 0.)
        return values

    def test_weighted_permits_and_oversized_request(self):
        self.assertFalse(self.arrive("share", 6.25)["is_heavy_read"])
        self.assertFalse(self.arrive("low", .9)["is_heavy_read"])
        self.assertTrue(self.arrive("ten", 10)["is_heavy_read"])
        self.assertTrue(self.arrive("fifteen", 15)["is_heavy_read"])
        self.assertTrue(self.arrive("other", 15)["is_heavy_read"])
        self.assertTrue(self.arrive("huge", 35)["is_heavy_read"])
        # P75 is now larger than 10, but the already-downstream mark remains.
        self.assertTrue(self.ledger.is_heavy(self.ledger.records["ten"].profile))
        self.assertIsNone(self.ledger.cap)

        one = self.acquire("fifteen")
        self.assertTrue(one["granted"])
        denied = self.acquire("other")
        self.assertFalse(denied["granted"])
        self.assertEqual(denied["held_Bps"], 15*GB)
        self.assertEqual(denied["remaining_Bps"], 10*GB)
        self.assertTrue(self.acquire("ten")["granted"])
        self.assertEqual(self.ledger.held_Bps, 25*GB)
        self.assertFalse(self.acquire("huge")["granted"])
        self.release("ten")
        self.assertEqual(self.ledger.held_Bps, 15*GB)
        self.assertFalse(self.acquire("huge")["granted"])
        self.release("fifteen")
        self.assertTrue(self.acquire("huge")["granted"])
        self.assertEqual(self.ledger.held_Bps, 35*GB)
        self.assertEqual(self.acquire("other")["remaining_Bps"], 0.)
        self.release("huge")
        self.assertTrue(self.acquire("other")["granted"])
        self.release("other")
        results = self.audit()
        self.assertEqual(results["peak_held_Bps"], 35*GB)
        self.assertEqual(results["oversized_alone_grants"], 1)

    def test_rollback_and_duplicate_grant(self):
        hint = self.arrive("a", 15)
        self.assertEqual(hint_from_request(attach_http_hint({}, hint)["nvext"]), hint)
        first = self.acquire("a")
        self.assertEqual(self.acquire("a")["permit_id"], first["permit_id"])
        self.assertEqual(self.ledger.held_Bps, 15*GB)
        self.ledger.rollback("a", 0, first["permit_id"], .2)
        self.ledger.rollback("a", 0, first["permit_id"], .2)
        self.assertEqual(self.ledger.held_Bps, 0.)
        self.assertTrue(self.acquire("a", key="new")["granted"])
        self.release("a")
        self.audit()

    def test_fractional_pressure_uses_integer_wire_and_ledger(self):
        hint = self.arrive("fractional", 10.123456789123)
        self.assertIs(type(hint["pressure_Bps"]), int)
        self.assertEqual(hint["pressure_Bps"], 10123456789)
        self.assertTrue(self.acquire("fractional")["granted"])
        self.assertEqual(self.ledger.held_Bps, hint["pressure_Bps"])
        self.release("fractional")
        self.audit()

    def test_event_rollover_keeps_full_offline_history(self):
        import sim
        config_path = Path(sim.__file__).resolve().parents[1] / "configs/demo.json"
        config = json.loads(config_path.read_text())
        config["scheduler"] = "upstream"
        config["admission"] = dict(self.config, classification="arrival", aging_enabled=False)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            state = AdmissionState(config, folder / "admission.events.jsonl")
            with state.log:
                for seq in range(10000):
                    state.log_event("sample", seq=seq)
                state.controller.logs.append(dict(event="last", seq=10000))
                online = state.snapshot()  # Drains the controller's last event.
            self.assertEqual([e["seq"] for e in online["events"]], list(range(1, 10001)))
            offline = save_admission_snapshot(folder, online, full_event_log=True)
            self.assertEqual([e["seq"] for e in offline["events"]], list(range(10001)))
            raw = json.loads((folder / "admission.runtime.final.json").read_text())
            self.assertEqual(raw["events"], online["events"])


if __name__ == "__main__":
    unittest.main()
