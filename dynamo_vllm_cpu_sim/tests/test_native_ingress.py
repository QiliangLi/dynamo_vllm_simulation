import asyncio
import copy
import json
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import httpx
from fastapi import FastAPI

RUNTIME = Path(__file__).resolve().parents[1]

from sim.stack.ingress_control import IngressControl, ProtocolError, install_routes
from sim.request_profile import normalize_config
from sim.stack.admission_service import AdmissionState


def config():
    c = json.loads((RUNTIME / 'configs/demo.json').read_text())
    c.update(router_mode='kv', scheduler='upstream', block_size=16,
             max_model_len=4096)
    c['admission'] = dict(mode='tfs', placement='scheduler',
        classification='arrival', aging_enabled=False, profile_source='dynamo_tier',
        reference_bandwidth_Bps=1)
    return normalize_config(c)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = AdmissionState(config())
        self.clock = self.state.origin + 10
        self.state.now = lambda: self.state.clock.to_model(self.clock - self.state.origin)
        self.api = IngressControl(self.state, 'test-clock',
                                  monotonic=lambda: self.clock)
        self.rid = str(uuid.uuid4())
        self.begin = dict(epoch=self.api.epoch, request_id=self.rid,
            attempt_id=str(uuid.uuid4()), clock_id='test-clock',
            arrival_mono=self.state.origin + 1, model='minimax')
        self.profile = dict(epoch=self.api.epoch, request_id=self.rid,
            snapshot=dict(version=1, request_id=self.rid, prompt_tokens=1024,
                          block_size=16, disk_blocks=32))

    def prepare(self):
        self.api.begin(self.begin)
        return self.api.profile(self.profile)

    def test_minimax_config_and_fit_reach_the_native_profile_log(self):
        cfg = normalize_config(json.loads((RUNTIME / 'configs/minimax_native_ingress.json').read_text()))
        cfg['routing_accounting']['enabled'] = False
        with patch.dict(os.environ, {'SIM_STACK_ROUTING_SCORE_READY': 'unused-score-ready.json'}):
            state = AdmissionState(cfg)
        clock = state.origin + 2
        state.now = lambda: state.clock.to_model(clock - state.origin)
        api = IngressControl(state, 'test-clock', monotonic=lambda: clock)
        begin = dict(self.begin, epoch=api.epoch, arrival_mono=state.origin + 1)
        api.begin(begin)
        snapshot = dict(version=1, request_id=self.rid, prompt_tokens=61696,
                        block_size=128, disk_blocks=480)
        api.profile(dict(epoch=api.epoch, request_id=self.rid, snapshot=snapshot))
        profile = state.controller.records[uuid.UUID(self.rid).hex].profile
        row, = [event for event in state.events
                if event['event'] == 'dynamo_tier_profile_resolved']
        self.assertEqual(profile.estimated_read_bytes, 4026531840)
        self.assertEqual((row['hit_tokens'], row['miss_tokens']), (61440, 256))
        # Verify propagation of the configured model, without changing its version.
        from sim.reference_time import get_reference_time_estimator
        expected = get_reference_time_estimator(cfg).estimate(
            61696, 61440, remote_read_tokens=61440)
        self.assertGreater(expected.compute_s, 0)
        self.assertTrue(expected.compute_time_source)
        self.assertRegex(expected.model_sha256 or '', r'^[0-9a-f]{64}$')
        self.assertAlmostEqual(row['standalone_compute_s'], expected.compute_s, places=9)
        self.assertAlmostEqual(profile.standalone_compute_s, expected.compute_s, places=9)
        self.assertEqual(row['compute_time_source'], expected.compute_time_source)
        self.assertEqual(row['model_sha256'], expected.model_sha256)

    def acquire(self):
        rid = uuid.UUID(self.rid).hex
        return self.state.control(dict(op='acquire', request_id=self.rid,
            worker_id=0, acquisition_key='attempt-1', load_attempt_id=1,
            storage_hint=self.state.controller.hints[rid]))

    def event(self, kind='first_token', sequence=1):
        evidence = dict(first_token='nonempty_text',
                        request_finished='finish_reason',
                        uncertain='execution_not_confirmed')[kind]
        return dict(epoch=self.api.epoch, request_id=self.rid, sequence=sequence,
                    kind=kind, occurred_mono=self.state.origin + 2 + sequence,
                    evidence=evidence)

    def test_metadata_registration_does_not_build_fake_tokens_or_reserve(self):
        result = self.api.begin(self.begin)
        self.assertTrue(result['ok'])
        self.assertFalse(self.state.futures)
        self.assertFalse(self.state.tier_pending)
        self.assertFalse(self.state.controller.records)
        self.assertEqual(self.state.controller.held, 0)
        self.assertEqual(self.api.begin(self.begin), result)

    def test_arrival_and_existing_model_are_preserved(self):
        self.prepare()
        record = self.state.controller.records[uuid.UUID(self.rid).hex]
        self.assertAlmostEqual(record.profile.arrival_s, 1)
        self.assertEqual(record.profile.prompt_tokens, 1024)
        self.assertEqual(record.profile.estimated_remote_tokens, 512)
        self.assertEqual(self.state.controller.held, 0)
        version = self.state.controller.threshold_version
        self.api.profile(self.profile)
        self.assertEqual(self.state.controller.threshold_version, version)

    def test_actual_controller_releases_once_on_first_text(self):
        self.prepare()
        self.assertTrue(self.acquire()['granted'])
        self.assertEqual(self.state.controller.held, 1)
        event = self.event()
        self.api.event(event)
        self.assertEqual(self.state.controller.held, 0)
        self.api.event(event)
        self.api.event(self.event('request_finished', 2))
        releases = [e for e in self.state.events if e['event'] == 'admission_released']
        self.assertEqual(len(releases), 1)

    def test_confirmed_empty_completion_releases_without_fake_ttft(self):
        self.prepare()
        self.acquire()
        self.api.event(self.event('request_finished'))
        record = self.state.controller.records[uuid.UUID(self.rid).hex]
        self.assertIsNone(record.first_token_s)
        self.assertEqual(record.state, 'FINISHED')
        self.assertEqual(self.state.controller.held, 0)

    def test_unknown_execution_releases_permit_without_global_failure(self):
        self.prepare()
        self.acquire()
        event = self.event('uncertain')
        self.api.event(event)
        self.assertEqual(self.state.controller.held, 0)
        self.assertIsNone(self.state.fatal)
        self.assertTrue(self.api.registrations[self.rid].uncertain)
        self.api.event(event)  # Retry of acknowledged event remains idempotent.
        self.assertEqual(self.api.hello()['uncertain_policy'],'release-permit-best-effort-v1')
        uncertain=[e for e in self.state.events if e['event']=='upstream_uncertain']
        self.assertEqual(len(uncertain),1)
        self.assertEqual(uncertain[0]['scope'],'request')
        self.assertEqual(sum(e['event']=='admission_released' for e in self.state.events), 1)
        self.assertIsNone(self.state.controller.records[uuid.UUID(self.rid).hex].finish_s)

    def register_other(self):
        other=str(uuid.uuid4())
        self.api.begin(dict(self.begin,request_id=other,attempt_id=str(uuid.uuid4())))
        snapshot=dict(self.profile['snapshot'],request_id=other)
        self.api.profile(dict(self.profile,request_id=other,snapshot=snapshot))
        return other

    def other_acquire(self, other):
        return self.state.control(dict(op='acquire',request_id=other,worker_id=1,
            acquisition_key='other-attempt',load_attempt_id=1,
            storage_hint=self.state.controller.hints[uuid.UUID(other).hex]))

    def test_other_worker_can_acquire_and_complete_after_uncertain_request(self):
        self.prepare()
        self.acquire()
        self.api.event(self.event('uncertain'))
        other=self.register_other()
        self.state.controller.cap=2
        self.assertTrue(self.other_acquire(other)['granted'])
        self.assertEqual(self.state.controller.held,1)
        self.api.event(dict(self.event('request_finished'),request_id=other))
        self.assertEqual(self.state.controller.held,0)
        self.assertFalse(self.state.controller.records[uuid.UUID(self.rid).hex].lease_open)
        self.assertIsNone(self.state.fatal)

    def test_released_permit_does_not_block_next_request(self):
        self.prepare()
        self.acquire()
        self.api.event(self.event('uncertain'))
        other=self.register_other()
        self.state.controller.cap=1
        reply=self.other_acquire(other)
        self.assertTrue(reply['ok'])
        self.assertTrue(reply['granted'])
        self.assertEqual(reply['reason'],'granted')
        self.assertIsNone(self.state.fatal)

    def test_uncertain_after_first_token_does_not_reopen_released_permit(self):
        self.prepare()
        self.acquire()
        self.api.event(self.event())
        self.api.event(self.event('uncertain',2))
        self.assertEqual(self.state.controller.held,0)
        self.assertIsNone(self.state.fatal)

    def test_uncertain_before_profile_does_not_create_permit_or_stop_other_requests(self):
        self.api.begin(self.begin)
        self.api.event(self.event('uncertain'))
        self.assertEqual(self.state.controller.held,0)
        self.assertFalse(self.state.controller.records)
        self.register_other()
        self.assertIsNone(self.state.fatal)

    def test_late_worker_acquire_after_uncertainty_cannot_reopen_quota(self):
        self.prepare()
        self.api.event(self.event('uncertain'))
        reply = self.acquire()
        self.assertFalse(reply['granted'])
        self.assertTrue(reply['bypass'])
        self.assertIsNone(reply['permit_id'])
        self.assertEqual(self.state.controller.held, 0)
        self.assertIsNone(self.state.fatal)

    def test_profile_exception_is_request_local(self):
        self.api.begin(self.begin)
        with patch('sim.stack.tier_profile.build_tier_profile', side_effect=ValueError('bad fit')):
            result = self.api.profile(self.profile)
        self.assertTrue(result['degraded'])
        self.assertFalse(result['is_heavy_read'])
        self.assertIsNone(self.state.fatal)
        self.api.event(self.event('request_finished'))
        self.register_other()

    def test_expired_permit_and_late_feedback_release_only_once(self):
        self.prepare()
        self.acquire()
        self.clock += 121
        self.state.admit()
        self.assertEqual(self.state.controller.held, 0)
        self.assertTrue(self.acquire()['bypass'])
        self.api.event(self.event('first_token'))
        self.api.event(self.event('request_finished', 2))
        releases = [r for r in self.state.events if r['event'] == 'admission_released']
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]['reason'], 'permit_expired')

    def test_terminal_history_pruned_but_active_registration_kept(self):
        self.prepare()
        self.acquire()
        self.api.event(self.event('uncertain'))
        self.clock += 601
        self.api.prune()
        self.assertNotIn(self.rid, self.api.registrations)
        self.assertFalse(self.state.controller.permits)
        self.assertFalse(self.state.aliases)
        with self.assertRaises(ProtocolError):
            self.api.event(self.event('uncertain'))
        with self.assertRaises(ProtocolError):
            self.api.begin(self.begin)  # Old retries cannot revive a pruned identity.

    def test_old_epoch_cannot_mutate_state(self):
        self.begin['epoch'] = 'old'
        with self.assertRaises(ProtocolError):
            self.api.begin(self.begin)
        self.assertFalse(self.state.aliases)

    def test_clock_mismatch_and_invalid_arrival_rejected_before_mutation(self):
        for fields in [dict(clock_id='other'), dict(arrival_mono=True),
                       dict(arrival_mono=float('nan')),
                       dict(arrival_mono=self.clock + 1)]:
            with self.subTest(fields=fields), self.assertRaises(ProtocolError):
                self.api.begin(dict(self.begin, **fields))
        self.assertFalse(self.state.aliases)

    def test_uuid_reuse_is_not_an_idempotent_retry(self):
        self.api.begin(self.begin)
        with self.assertRaises(ProtocolError):
            self.api.begin(dict(self.begin, attempt_id=str(uuid.uuid4())))

    def test_changed_profile_rejected(self):
        self.prepare()
        value = copy.deepcopy(self.profile)
        value['snapshot']['disk_blocks'] = 0
        with self.assertRaises(ProtocolError):
            self.api.profile(value)

    def test_spoofed_snapshot_identity_rejected(self):
        self.api.begin(self.begin)
        value = copy.deepcopy(self.profile)
        value['snapshot']['request_id'] = str(uuid.uuid4())
        with self.assertRaises(ProtocolError):
            self.api.profile(value)
        self.assertFalse(self.state.controller.records)

    def test_out_of_order_conflicting_and_post_terminal_events(self):
        self.prepare()
        with self.assertRaises(ProtocolError):
            self.api.event(self.event('request_finished', 2))
        event = self.event()
        self.api.event(event)
        with self.assertRaises(ProtocolError):
            self.api.event(dict(event, occurred_mono=self.state.origin + 4))
        self.api.event(self.event('request_finished', 2))
        with self.assertRaises(ProtocolError):
            self.api.event(self.event('uncertain', 3))

    def test_done_marker_is_not_completion_evidence(self):
        self.prepare()
        self.acquire()
        with self.assertRaises(ProtocolError):
            self.api.event(dict(self.event('request_finished'), evidence='[DONE]'))
        self.assertEqual(self.state.controller.held, 1)

    def test_request_payload_is_never_accepted(self):
        with self.assertRaises(ProtocolError):
            self.api.begin(dict(self.begin, prompt='secret text'))
        self.assertFalse(self.state.aliases)

    def test_capacity_rejects_without_evicting_a_live_request(self):
        self.api.capacity = 1
        self.api.begin(self.begin)
        with self.assertRaises(ProtocolError):
            self.api.begin(dict(self.begin, request_id=str(uuid.uuid4())))
        self.assertIn(self.rid, self.api.registrations)

    async def test_http_auth_size_and_routes(self):
        app = FastAPI()
        with patch('sim.stack.admission_rpc.clock_id',return_value='test-clock'):
            api = install_routes(app, self.state, 'test-secret')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url='http://central') as client:
            url = '/internal/dynamo-ingress/hello'
            self.assertEqual((await client.get(url)).status_code, 403)
            headers = {'x-tier-profile-token': 'test-secret'}
            response = await client.get(url, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['epoch'], api.epoch)
            response = await client.post('/internal/dynamo-ingress/begin',
                content=b'x' * 16385, headers=headers)
            self.assertEqual(response.status_code, 413)


if __name__ == '__main__':
    unittest.main()
