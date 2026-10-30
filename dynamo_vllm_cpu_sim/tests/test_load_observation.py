"""Execution logging cannot call the ledger; offline audit still checks real loads."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch
import uuid

from sim.admission import AdmissionRecord
from sim.stack.clock import TimeScale
from sim.stack.layerwise import LayerRunner
from sim.stack.load_observation import LoadObserver, with_local_observations
from sim.stack.storage_service import Service
from test_layerwise import config, Clock


def status(identities):
    return dict(execution_feedback='disabled', origin_monotonic_s=time.monotonic()-1,
        wall_time_scale=1., records={rid: asdict(AdmissionRecord(None, worker_id=-1)) for rid in identities},
        events=[dict(event='request_correlated', id=rid, engine_uuid=engine)
                for rid, engine in identities.items()])


class LocalLoadTests(unittest.TestCase):
    def test_post_score_refresh_keeps_report_observations_and_raw_ledger_separate(self):
        from scripts.run_tfs_stack import save_admission_snapshot
        engine=str(uuid.uuid4())
        raw=status({'r':engine})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            observer=LoadObserver(folder/'worker0.loads.jsonl',0)
            try:
                observer.starting(engine)
                save_admission_snapshot(folder,raw)
                raw['routing_score']={'active':0}
                save_admission_snapshot(folder,raw)
                online=json.loads((folder/'admission.runtime.final.json').read_text())
                report=json.loads((folder/'admission.final.json').read_text())
                self.assertEqual(report['load_observation_source'],'offline_worker_logs')
                self.assertTrue(report['records']['r']['no_external_load'])
                self.assertFalse(online['records']['r']['no_external_load'])
                self.assertEqual(online['records']['r']['worker_id'],-1)
                self.assertEqual(report['records']['r']['worker_id'],0)
                self.assertEqual(report['routing_score']['active'],0)
                self.assertFalse(raw['records']['r']['no_external_load'])
            finally: observer.close()

    def test_pipeline_and_resident_chunk_never_open_ledger_client(self):
        clock, cfg = Clock(), config()
        with tempfile.TemporaryDirectory() as tmp, \
             patch('time.monotonic', side_effect=lambda: clock.now), \
             patch('time.perf_counter', side_effect=lambda: clock.now), \
             patch('time.sleep', side_effect=clock.sleep), \
             patch('sim.stack.feedback.feedback_client', side_effect=AssertionError('unexpected ledger RPC')):
            path = Path(tmp)/'worker0.loads.jsonl'
            observer = LoadObserver(path, 0)
            try:
                service = Service(cfg, [])
                service.store.present.add(b'a')
                state = service.layer_state
                load = state.register(0, 'r', [b'a'.hex()], 100.)
                runner = LayerRunner(cfg, SimpleNamespace(layer_submit=state.submit,
                    layer_status=state.status), 0, TimeScale(1), observer, lambda *a, **kw: None)
                runner.install(SimpleNamespace(loads={'r': load}))
                timing = dict(mode='linear', duration_s=.03)
                runner.run(['r'], timing, 1)
                before = path.read_bytes()
                runner.run(['r'], timing, 2)
                self.assertEqual(path.read_bytes(), before)
                rows = [json.loads(line) for line in before.splitlines()]
                self.assertEqual([r['kind'] for r in rows],
                    ['load_submitted']+['layer_load_submitted']*3+['load_completed'])
                self.assertEqual(service.bytes_submitted, 300)
                self.assertAlmostEqual(service.store.bytes_transferred, 300)
            finally:
                observer.close()

    def test_executor_initialization_and_no_load_execution_do_not_connect_to_ledger(self):
        from sim.stack.executor import SimExecutor
        cfg = config('bulk')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict('os.environ', SIM_STACK_STEP_LOG=str(Path(tmp)/'worker0.steps.jsonl')), \
             patch('sim.stack.executor.load_stack_config', return_value=cfg), \
             patch('sim.stack.storage_client.client_from_env', return_value=None), \
             patch('sim.stack.feedback.feedback_client', side_effect=AssertionError('unexpected ledger RPC')):
            executor = SimExecutor.__new__(SimExecutor)
            executor._init_executor()
            try:
                executor._load_observer.starting('r')
                rows = [json.loads(s) for s in next(Path(tmp).glob('*.loads.jsonl')).read_text().splitlines()]
                self.assertEqual([r['kind'] for r in rows], ['no_external_load'])
            finally:
                executor.shutdown()

    def test_offline_join_keeps_runtime_ledger_unchanged(self):
        ids = {rid: str(uuid.uuid4()) for rid in ('read', 'cold')}
        raw = status(ids)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'worker0.loads.jsonl'
            with_observer = LoadObserver(path, 0)
            try:
                with_observer.submitted(ids['read'], 0, layer_count=3, expected_read_bytes=300)
                with_observer.starting(ids['read'])
                for layer in range(3):
                    with_observer.layer_submitted(ids['read'], layer, 100)
                with_observer.completed(ids['read'])
                with_observer.starting(ids['cold'])
                joined = with_local_observations(raw, [path])
            finally:
                with_observer.close()
        self.assertEqual(raw['records']['read']['submitted_read_bytes'], 0)
        self.assertEqual(raw['records']['read']['worker_id'], -1)
        self.assertEqual(joined['records']['read']['submitted_read_bytes'], 300)
        self.assertEqual(joined['records']['read']['load_attempts'], 1)
        self.assertIsNotNone(joined['records']['read']['load_done_s'])
        self.assertTrue(joined['records']['cold']['no_external_load'])
        self.assertEqual(joined['load_observation_source'], 'offline_worker_logs')

    def test_incomplete_layers_missing_logs_and_wrong_worker_fail_audit(self):
        for failure in ('incomplete', 'missing', 'worker'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                engine = str(uuid.uuid4())
                raw = status({'r': engine})
                path = Path(tmp)/'worker0.loads.jsonl'
                observer = LoadObserver(path, 0)
                try:
                    if failure == 'worker':
                        raw['records']['r']['worker_id'] = 1
                        observer.starting(engine)
                    elif failure == 'incomplete':
                        observer.submitted(engine, 0, layer_count=3, expected_read_bytes=300)
                        observer.layer_submitted(engine, 0, 100)
                        observer.completed(engine)
                    with self.assertRaises(ValueError):
                        with_local_observations(raw, [path])
                finally:
                    observer.close()


if __name__ == '__main__':
    unittest.main()
