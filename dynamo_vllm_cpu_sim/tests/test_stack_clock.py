"""Time dilation contracts across storage, admission, executor and HTTP output."""
import asyncio
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from sim.stack.clock import TimeScale


class TimeScaleTest(unittest.TestCase):
    def test_units_and_invalid_factors(self):
        for factor in (1, 10., 100):
            clock = TimeScale(factor)
            self.assertAlmostEqual(clock.to_model(clock.to_wall(.003)), .003)
        for factor in (True, False, 0, .5, -1, float("inf"), float("nan"), "10"):
            with self.assertRaises(ValueError):
                TimeScale(factor)

    def test_storage_bandwidth_and_model_completion_are_invariant(self):
        from sim.stack.storage_service import Service
        for factor in (1., 10., 100.):
            cfg = dict(block_size=1,wall_time_scale=factor, storage=dict(
                disks=1,paths_per_disk=4,disk_Bps=1000.,path_Bps=1000.,
                shared_link_Bps=None,kv_bytes_per_token=100,read_latency_s=.01,
                bandwidth_policy="max_min_fair",record_reads=True))
            with mock.patch("time.monotonic",return_value=100.) as mono:
                service = Service(cfg,[])
                service.store.present.add(b"a")
                service.op_submit(0,"r",[b"a".hex()],100.)
                mono.return_value = 100.+factor*.06
                self.assertEqual(service.op_finished(0)["rids"],[])
                self.assertAlmostEqual(service.store.bytes_transferred,50.)
                mono.return_value = 100.+factor*.12
                self.assertEqual(service.op_finished(0)["rids"],["r"])
                self.assertEqual(service.store.bytes_transferred,100.)
                self.assertEqual(service.store.c["disk_Bps"],1000.)
                self.assertAlmostEqual(service.read_history[0]["completed_s"],.11)

    def test_executor_slows_sleep_but_keeps_compute_formula(self):
        from sim.stack.executor import SimExecutor
        for factor in (1.,10.,100.):
            executor = SimExecutor.__new__(SimExecutor)
            executor.clock = TimeScale(factor)
            executor.compute = dict(step_overhead_s=.001,prefill_token_s=.002,decode_token_s=.003)
            executor._prompt_len,executor._base_computed = {},{}
            executor._feedback = executor._storage = None
            executor.steps,executor.sleep_s = 0,0.
            executor._step_log = io.StringIO()
            out = SimpleNamespace(
                scheduled_new_reqs=[SimpleNamespace(req_id="r",prompt_token_ids=[1,2],num_computed_tokens=0)],
                scheduled_cached_reqs=SimpleNamespace(req_ids=[],num_computed_tokens=[],num_output_tokens=[]),
                num_scheduled_tokens={"r":2},finished_req_ids=[])
            with mock.patch("time.sleep") as sleep, mock.patch(
                    "time.perf_counter",side_effect=[100.,100.+.005*factor]):
                result = executor.execute_model(out).result()
            sleep.assert_called_once_with(.005*factor)
            self.assertEqual(result.req_ids,["r"])
            record = json.loads(executor._step_log.getvalue())
            self.assertAlmostEqual(record["timing"]["duration_s"],.005)
            self.assertAlmostEqual(record["sleep_elapsed_model_s"],.005)
            self.assertAlmostEqual(record["sleep_requested_wall_s"],.005*factor)


class AsyncTimeScaleTest(unittest.IsolatedAsyncioTestCase):
    async def test_feedback_delay_and_profiles_use_model_time(self):
        from sim.stack.admission_service import AdmissionState
        root = Path(__file__).resolve().parents[1]
        profiles = []
        for factor in (1.,10.,100.):
            cfg = json.loads((root/"configs/demo.json").read_text())
            cfg["wall_time_scale"] = factor
            cfg["admission"] = dict(mode="tfs",reference_bandwidth_Bps=1.,
                release_on="load_done",feedback_delay_s=.02,aging_enabled=False)
            with mock.patch("time.monotonic",return_value=100.) as mono:
                state = AdmissionState(cfg)
                state.arrive(dict(id="a",prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
                state.arrive(dict(id="b",prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
                state.admit()
                profiles.append(state.controller.records["a"].profile)
                self.assertTrue(state.futures["a"].done())
                self.assertFalse(state.futures["b"].done())
                mono.return_value = 100.+factor*.01
                state.receive(dict(request_id=state.uuid_by_id["a"]+"_0",event_id="e",
                    kind="no_external_load",worker_id=1,occurred_mono=mono.return_value,payload={}))
                mono.return_value = 100.+factor*.029
                state.admit()
                self.assertFalse(state.futures["b"].done())
                mono.return_value = 100.+factor*.031
                state.admit()
                self.assertTrue(state.futures["b"].done())
                received = next(e for e in state.events if e["event"]=="feedback_received")
                self.assertAlmostEqual(received["occurred_s"],.01)
                self.assertAlmostEqual(received["visible_s"],.03)
        self.assertTrue(all(p==profiles[0] for p in profiles))

    async def test_http_ttft_retains_wall_and_model_units(self):
        from scripts.replay_http import one_request

        class Response:
            def raise_for_status(self):
                pass
            async def aiter_lines(self):
                yield 'data: {"choices":[{"text":"x","finish_reason":"stop"}]}'
        class Client:
            @asynccontextmanager
            async def stream(self,*args,**kwargs):
                yield Response()
        for factor in (1.,10.,100.):
            records = {}
            with mock.patch("time.perf_counter",side_effect=[100.,100.+.02*factor,100.+.03*factor]):
                await one_request(Client(),"http://localhost/v1/completions","m","r","x",1,
                                  records,"completions",time_scale=factor)
            self.assertTrue(records["r"]["ok"],records)
            self.assertAlmostEqual(records["r"]["ttft_s"],.02)
            self.assertAlmostEqual(records["r"]["e2e_s"],.03)
            self.assertAlmostEqual(records["r"]["ttft_wall_s"],.02*factor)


if __name__ == "__main__":
    unittest.main()
