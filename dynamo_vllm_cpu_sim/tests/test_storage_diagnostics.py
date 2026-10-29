"""Diagnostic wrappers must preserve storage decisions and expose causal order."""
import copy
import unittest
from unittest import mock

from scripts.diagnose_storage_submit import Instrumentation, peak_overlap, prepare, rpc_trial


CONFIG = dict(block_size=1, cache_model="first_seen", enable_prefix_caching=False,
              storage=dict(disks=1, paths_per_disk=4, disk_Bps=120000000000.,
                           path_Bps=2000000000., shared_link_Bps=None,
                           kv_bytes_per_token=109824, read_latency_s=.0001,
                           bandwidth_policy="max_min_fair", record_reads=True))


class StorageDiagnosticsTest(unittest.TestCase):
    def test_overlap_uses_half_open_intervals(self):
        self.assertEqual(peak_overlap([
            dict(submitted_s=0, completed_s=1),
            dict(submitted_s=1, completed_s=2)]), 1)
        self.assertEqual(peak_overlap([
            dict(submitted_s=0, completed_s=2),
            dict(submitted_s=1, completed_s=3)]), 2)

    def test_wrappers_preserve_event_times_and_bytes(self):
        outcomes = []
        for enabled in [False, True]:
            with mock.patch("time.monotonic", return_value=100.):
                service, specs, demand = prepare(CONFIG, 2, 4, 0)
                inst = Instrumentation(service) if enabled else None
                for spec in specs:
                    row = inst.begin("submit", spec["rid"]) if inst else None
                    service.op_submit(spec["worker"], spec["rid"], spec["blocks_hex"], demand)
                    if inst:
                        inst.end(row)
                while service.store.pending:
                    service._drain_done(service.store.advance(service.store.next_time()))
                outcomes.append((copy.deepcopy(service.read_history), service.store.bytes_transferred))
                if inst:
                    self.assertEqual(len(inst.completions), 2)
        self.assertEqual(outcomes[0], outcomes[1])

    def test_real_rpc_timestamps_match_responses(self):
        trial = rpc_trial(CONFIG, 2, 4, 0, True, True)
        self.assertEqual(trial["pending"], 0)
        self.assertAlmostEqual(trial["bytes_transferred"], 2 * 4 * 109824, delta=.001)
        for client in trial["clients"]:
            for call in client["calls"]:
                # present_for_request carries rid but no worker in the real
                # protocol; finished carries worker but no rid.
                server = next(r for r in trial["server_rows"]
                              if r["sequence"] == call["sequence"] and
                              (r["rid"] == client["rid"] or r["worker"] == call["worker"]))
                self.assertLessEqual(call["called_ns"], server["framed_ns"])
                self.assertLessEqual(server["framed_ns"], server["decoded_ns"])
                self.assertLessEqual(server["decoded_ns"], server["acquired_ns"])
                self.assertLessEqual(server["acquired_ns"], server["operation_done_ns"])
                self.assertLessEqual(server["operation_done_ns"], call["decoded_ns"])
        self.assertEqual(len(trial["reads"]), 2)


if __name__ == "__main__":
    unittest.main()
