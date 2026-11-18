"""Unlimited admission keeps heavy classification and permit accounting."""
import unittest

import test_read_admission as late
import test_stack_admission as base
from sim.request_profile import normalize_config
from sim.stack.admission_service import AdmissionState


class DevModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_keeps_permits_and_completion(self):
        config = base.config()
        config["scheduler"] = "upstream"
        config["workers"] = 3
        config["admission"].update(placement="scheduler", classification="arrival", dev_mode=True)
        state = AdmissionState(config)
        for rid in ("a", "b", "c"):
            base.StackAdmissionTests.arrive(self, state, rid)
        state.admit()
        self.assertIsNone(state.controller.cap)
        permits = []
        for worker, rid in enumerate(("a", "b", "c")):
            self.assertTrue(state.controller.hints[rid]["is_heavy_read"])
            reply = late.LateLedgerTests.acquire(self, state, rid, worker)
            self.assertTrue(reply["granted"])
            permits.append(reply["permit_id"])
            self.assertEqual(reply["permit_id"],
                             late.LateLedgerTests.acquire(self, state, rid, worker)["permit_id"])
        self.assertEqual(state.controller.held, 3)
        state.local_event("request_finished", "b")
        state.local_event("request_finished", "b")
        self.assertEqual(state.controller.held, 2)
        late.LateLedgerTests.rollback(self, state, "c", permits[2], worker=2)
        state.local_event("first_token", "a")
        self.assertEqual(state.controller.held, 0)
        self.assertEqual(state.controller.permits[permits[1]]["state"], "released")
        self.assertIsNone(state.controller.snapshot(state.now()).heavy_cap)

    async def test_gateway_unlimited(self):
        config = base.config()
        config["admission"]["dev_mode"] = True
        state = AdmissionState(config)
        futures = [base.StackAdmissionTests.arrive(self, state, rid) for rid in ("a", "b")]
        state.admit()
        self.assertTrue(all(f.done() for f in futures))
        self.assertEqual(state.controller.held, 2)

    def test_dev_flag_validation(self):
        self.assertFalse(normalize_config(base.config())["admission"]["dev_mode"])
        config = base.config()
        config["admission"]["dev_mode"] = "false"
        with self.assertRaises(ValueError):
            normalize_config(config)


if __name__ == "__main__":
    unittest.main()
