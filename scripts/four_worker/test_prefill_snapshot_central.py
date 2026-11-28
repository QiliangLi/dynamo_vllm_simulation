"""Central prefill snapshot tests through native ingress callbacks."""
import time
import unittest
import uuid

from check_read_admission import check_prefill_ledger
from sim.stack.admission_service import AdmissionState
from sim.stack.server_ingress_control import IngressControl


class PrefillSnapshotCentralTests(unittest.TestCase):
    def setup_state(self, *, weighted=False, force_light=False, idle_bypass=False):
        config = dict(workers=4, scheduler="upstream", router_mode="kv", policy="fcfs",
                      block_size=16, num_blocks=128, max_model_len=4096,
                      max_num_seqs=1, max_num_batched_tokens=128,
                      kv_transfer_mode="layer_pipeline",
                      storage=dict(disks=2, paths_per_disk=4, disk_Bps=12_500_000_000,
                                   shared_link_Bps=25_000_000_000, path_Bps=3_125_000_000,
                                   kv_bytes_per_token=131072, read_latency_s=.0001,
                                   bandwidth_policy="max_min_fair"),
                      compute=dict(step_overhead_s=.00005, prefill_token_s=.000004,
                                   decode_token_s=.00002),
                      admission=dict(mode="tfs", placement="scheduler", classification="arrival",
                                     aging_enabled=False, profile_source="dynamo_tier",
                                     prefill_bandwidth_selection=True, bandwidth_permits=weighted,
                                     force_light=force_light, idle_bypass=idle_bypass))
        self.state = AdmissionState(config)
        self.ingress = IngressControl(self.state, "test-clock")

    def profile(self, prompt_tokens=32, disk_blocks=1):
        identity = uuid.uuid4()
        rid = str(identity)
        self.ingress.begin(dict(epoch=self.ingress.epoch, request_id=rid,
                                attempt_id=str(uuid.uuid4()), clock_id="test-clock",
                                arrival_mono=time.monotonic(), model="test"))
        snapshot = dict(version=1, request_id=rid, prompt_tokens=prompt_tokens,
                        disk_blocks=disk_blocks, block_size=16)
        result = self.ingress.profile(dict(epoch=self.ingress.epoch, request_id=rid,
                                           snapshot=snapshot))
        self.assertFalse(result.get("degraded"), result)
        return rid, self.state.controller.hints[identity.hex]

    def control(self, op, rid, worker, **fields):
        return self.state.control(dict(op=op, request_id=rid, worker_id=worker, **fields))

    def start(self, rid, worker):
        return self.control("prefill_started", rid, worker, forced=False)

    def first(self, rid):
        self.ingress.event(dict(epoch=self.ingress.epoch, request_id=rid, sequence=1,
                                kind="first_token", occurred_mono=time.monotonic(),
                                evidence="nonempty_text"))

    def finish(self, rid):
        self.ingress.event(dict(epoch=self.ingress.epoch, request_id=rid, sequence=2,
                                kind="request_finished", occurred_mono=time.monotonic(),
                                evidence="finish_reason"))

    def audit(self):
        return check_prefill_ledger(self.state.snapshot(), self.state.config)

    def test_light_snapshot_is_idempotent_and_excludes_requesting_worker(self):
        self.setup_state(force_light=True)
        a, ah = self.profile()
        b, bh = self.profile(48)
        low, lowh = self.profile(1024)
        self.assertFalse(ah["is_heavy_read"])
        self.assertGreater(ah["pressure_Bps"], 1_000_000_000)
        self.assertLess(lowh["pressure_Bps"], 1_000_000_000)
        self.assertEqual(self.start(a, 0)["snapshot"], dict(version=1, other_active_Bps=0))
        self.assertEqual(self.start(a, 0)["snapshot"], dict(version=1, other_active_Bps=0))
        self.assertEqual(self.start(b, 1)["snapshot"],
                         dict(version=2, other_active_Bps=ah["pressure_Bps"]))
        self.first(a)
        late = self.start(a, 0)
        self.assertFalse(late["registered"])
        self.assertEqual(late["snapshot"], dict(version=3, other_active_Bps=bh["pressure_Bps"]))
        self.assertTrue(self.start(low, 2)["registered"])
        no_remote, no_remote_hint = self.profile()
        self.assertGreater(no_remote_hint["pressure_Bps"], 0)
        zero = self.control("prefill_started", no_remote, 3, forced=False, external_load=False)
        self.assertTrue(zero["registered"])
        self.assertEqual(zero["snapshot"],
                         dict(version=5, other_active_Bps=bh["pressure_Bps"] + lowh["pressure_Bps"]))
        self.assertEqual(self.state.prefill_snapshot(0), zero["snapshot"])
        self.finish(b)
        self.first(low)
        self.finish(no_remote)
        self.assertEqual(self.audit(), dict(prefill_registered=4, prefill_forced=0,
                                           prefill_late_notices=1, prefill_snapshot_version=8))

    def test_weighted_grant_and_denial_embed_active_snapshot(self):
        self.setup_state(weighted=True)
        a, ah = self.profile()
        b, bh = self.profile()
        kwargs = dict(load_attempt_id=1)
        grant = self.control("acquire", a, 0, storage_hint=ah, acquisition_key="a", **kwargs)
        self.assertTrue(grant["granted"])
        self.assertEqual(grant["snapshot"], dict(version=0, other_active_Bps=0))
        self.start(a, 0)
        denial = self.control("acquire", b, 1, storage_hint=bh, acquisition_key="b", **kwargs)
        self.assertFalse(denial["granted"])
        self.assertEqual(denial["snapshot"], dict(version=1, other_active_Bps=ah["pressure_Bps"]))
        self.assertEqual(denial["remaining_Bps"], 25_000_000_000-ah["pressure_Bps"])
        self.first(a)
        grant = self.control("acquire", b, 1, storage_hint=bh, acquisition_key="b", **kwargs)
        self.assertTrue(grant["granted"])
        self.assertEqual(grant["snapshot"], dict(version=2, other_active_Bps=0))
        self.first(b)
        self.assertEqual(self.audit()["prefill_registered"], 1)

    def test_combined_legacy_forced_notice_registers_once(self):
        self.setup_state(idle_bypass=True)
        rid, hint = self.profile()
        proof = dict(running_before=0, scheduled_tokens_before=0, nonheavy_candidates=0,
                     allocated=True, external_tokens=16, heavy_candidates=1)
        token = uuid.uuid4().hex
        args = dict(forced=True, storage_hint=hint, load_attempt_id=1, proof=proof,
                    acquisition_key="idle", occurred_mono=time.monotonic(), permit_id=token)
        first = self.control("prefill_started", rid, 0, **args)
        self.assertTrue(first["granted"] and first["registered"])
        self.assertEqual(first["permit_id"], token)
        self.assertEqual(first["snapshot"], self.control("prefill_started", rid, 0, **args)["snapshot"])
        self.first(rid)
        late = self.control("prefill_started", rid, 0, **args)
        self.assertFalse(late["registered"])
        self.assertEqual(late["snapshot"], dict(version=2, other_active_Bps=0))
        self.assertEqual(self.audit()["prefill_forced"], 1)


if __name__ == "__main__":
    unittest.main()
