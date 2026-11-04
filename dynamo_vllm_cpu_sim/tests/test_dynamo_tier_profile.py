"""Raw-tier accounting, no caller oracle, and late-permit lifecycle contracts."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from sim.request_profile import normalize_config
from sim.reference_time import get_reference_time_estimator
from sim.stack.admission_service import AdmissionState, create_app
from sim.stack.scheduler_admission import ReadGate
from sim.stack.tier_profile import build_tier_profile, validate_snapshot, preview_hint

ROOT = Path(__file__).resolve().parents[1]


def config():
    c = json.loads((ROOT/'configs/demo.json').read_text())
    c.update(router_mode='kv', scheduler='upstream', block_size=16, max_model_len=4096)
    c['admission'] = dict(mode='tfs', placement='scheduler', classification='arrival',
        aging_enabled=False, profile_source='dynamo_tier', reference_bandwidth_Bps=1)
    return normalize_config(c)


def snapshot(rid='request', **changes):
    return dict(version=1, request_id=rid, prompt_tokens=1024, block_size=16,
        worker_id=77, dp_rank=0, dp_device_blocks=[[0, 8]], gpu_blocks=8,
        host_pinned_blocks=16, disk_blocks=32, **changes)


def resolve(state,value):
    prepared=state.prepare_tier_profiles(value['request_id'],[value])
    return state.commit_tier_profile(value['request_id'],prepared['preview_id'],
                                     value['worker_id'],value['dp_rank'])


class ProfileTests(unittest.TestCase):
    def test_preview_matches_existing_arrival_classification_without_counting_alternatives(self):
        from sim.stack.read_admission import FrozenAdmission,FrozenSAAdmission
        c=config()
        profiles=[]
        for blocks in [0,0,0,32,16,64]:
            value=snapshot(); value.update(gpu_blocks=0,host_pinned_blocks=0,
                disk_blocks=blocks,dp_device_blocks=[[0,0]])
            profiles.append(build_tier_profile(value,c,str(len(profiles)),0.))
        for factory,mode in [(FrozenAdmission,'linear'),(FrozenSAAdmission,'sa_e26f')]:
            local={**c,'compute':{'mode':mode}}
            for count in [0,3,4,5]:
                for candidate in profiles[count:]:
                    history=profiles[:count]
                    controller=factory(c['admission'])
                    if history: controller.on_arrivals(history,0.)
                    mark,theta=preview_hint(candidate,sorted(p.pressure_Bps for p in history),local,7)
                    controller.on_arrivals([candidate],0.)
                    self.assertEqual(mark['is_heavy_read'],controller.hints[candidate.request_id]['is_heavy_read'])
                    self.assertEqual(theta,controller.theta)

    def test_raw_disk_extension_and_total_reuse_are_different(self):
        c = config()
        p = build_tier_profile(snapshot(), c, 'r', 2.)
        self.assertEqual(p.estimated_remote_tokens, 256)
        self.assertEqual(p.estimated_read_bytes, 256*c['storage']['kv_bytes_per_token'])
        timing = get_reference_time_estimator(c).estimate(1024, 512, remote_read_tokens=256)
        self.assertEqual(p.standalone_compute_s, timing.compute_s)
        self.assertEqual(p.reference_service_s, timing.service_s)
        self.assertEqual(p.deadline_s, 2.+c['request_profile']['slo_multiplier']*timing.service_s)

    def test_full_cached_prompt_still_leaves_final_token(self):
        s = snapshot(); s['disk_blocks'] = 64
        p = build_tier_profile(s, config(), 'r', 0.)
        self.assertEqual(p.estimated_remote_tokens, (63-16)*16)

    def test_host_only_reuse_has_no_disk_pressure(self):
        s = snapshot(); s['disk_blocks'] = s['host_pinned_blocks']
        p = build_tier_profile(s, config(), 'r', 0.)
        self.assertEqual(p.pressure_Bps, 0)
        self.assertEqual(p.estimated_read_bytes, 0)

    def test_bad_tier_snapshots_are_rejected(self):
        for key, value in [('disk_blocks', 65), ('host_pinned_blocks', 3),
            ('gpu_blocks', True), ('block_size', 1), ('dp_device_blocks', [[0,8],[1,9]])]:
            s = snapshot(); s[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_snapshot(s, config())

    def test_no_client_binding_model_in_tier_mode(self):
        c = config(); c['cache_model'] = 'first_seen'
        with self.assertRaisesRegex(ValueError, 'first_seen'):
            normalize_config(c)

    def test_reference_read_override_preserves_legacy_default(self):
        e = get_reference_time_estimator(config())
        self.assertEqual(e.estimate(1024,512), e.estimate(1024,512,remote_read_tokens=512))
        with self.assertRaises(ValueError):
            e.estimate(1024,512,remote_read_tokens=513)

    def test_lookup_bias_is_logged_without_changing_admission(self):
        for estimated, actual in [(0, 1), (64, 32), (32, 64), (64, 0)]:
            with self.subTest(estimated=estimated,actual=actual):
                client = Mock()
                gate = ReadGate(client, tier_kv_bytes_per_token=16)
                gate.log = Mock()
                hint = dict(version=1,is_heavy_read=estimated>0,
                    estimated_remote_read_bytes=estimated*16,threshold_version=1)
                r = SimpleNamespace(request_id='r',sampling_params=SimpleNamespace(
                    extra_args={'storage_hint':dict(hint)}))
                if estimated:
                    gate.permits['r'] = 'existing-permit'
                    gate.pending_allocation.add('r')
                gate.verify_tier_lookup(r, actual)
                client.control.assert_not_called()
                self.assertEqual(r.sampling_params.extra_args['storage_hint'],hint)
                self.assertEqual(gate.permits,{'r':'existing-permit'} if estimated else {})
                self.assertEqual(gate.pending_allocation,{'r'} if estimated else set())
                gate.log.assert_any_call('tier_lookup_mismatch',request_id='r',
                    expected_read_bytes=estimated*16,actual_read_bytes=actual*16)

    def test_equal_lookup_does_not_log_a_mismatch(self):
        gate = ReadGate(Mock(),tier_kv_bytes_per_token=16)
        gate.hint = Mock(return_value={'estimated_remote_read_bytes':64*16})
        gate.log = Mock()
        gate.verify_tier_lookup(SimpleNamespace(request_id='r'),64)
        gate.log.assert_not_called()


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_are_classified_before_selection_without_booking(self):
        s=AdmissionState(config())
        s.arrive(dict(id='a',prompt_token_ids=list(range(1024))))
        warm=snapshot(s.uuid_by_id['a'])
        cold=dict(warm,worker_id=88,gpu_blocks=0,host_pinned_blocks=0,
                  disk_blocks=0,dp_device_blocks=[[0,0]])
        prepared=s.prepare_tier_profiles(s.uuid_by_id['a'],[warm,cold])
        self.assertEqual([row['hint']['is_heavy_read'] for row in prepared['candidates']],[True,False])
        self.assertEqual(s.controller.records,{})
        self.assertEqual(s.controller.held,0)
        s.commit_tier_profile(s.uuid_by_id['a'],prepared['preview_id'],88,0)
        self.assertEqual(len(s.controller.records),1)
        self.assertEqual(s.controller.records['a'].profile.estimated_remote_tokens,0)
        self.assertFalse(s.controller.hints['a']['is_heavy_read'])

    async def test_selected_commit_keeps_pre_score_hint_after_other_history_updates(self):
        s=AdmissionState(config())
        for rid in ['a','b']:
            s.arrive(dict(id=rid,prompt_token_ids=list(range(1024))))
        value=snapshot(s.uuid_by_id['a'])
        prepared=s.prepare_tier_profiles(s.uuid_by_id['a'],[value])
        resolve(s,snapshot(s.uuid_by_id['b']))
        original=dict(prepared['candidates'][0]['hint'])
        s.commit_tier_profile(s.uuid_by_id['a'],prepared['preview_id'],77,0)
        self.assertEqual(s.controller.hints['a'],original)
        self.assertEqual(len(s.controller.records),2)

    async def test_standard_request_needs_no_simulation_metadata(self):
        app = create_app(config(), 'http://test-dynamo')
        state = app.state.admission
        app.state.tokenizer = SimpleNamespace(encode=lambda text: list(range(1024)))
        @asynccontextmanager
        async def stream(method, url, *, json, headers):
            self.assertNotIn('_sim_admission', json)
            self.assertNotIn('nvext', json)
            value = snapshot(headers['x-dynamo-request-id'])
            value.update(gpu_blocks=0,host_pinned_blocks=0,disk_blocks=0,dp_device_blocks=[[0,0]])
            resolve(state,value)  # Stand-in for the native callback.
            async def lines():
                yield 'data: {"choices":[{"text":"x","finish_reason":"stop"}]}'
            yield SimpleNamespace(raise_for_status=lambda:None, aiter_lines=lines)
        app.state.client = SimpleNamespace(stream=stream)
        app.state.producers = set()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            response = await c.post('/v1/completions',json=dict(
                prompt='hello',max_tokens=1,stream=True,model='sim-opt'))
            self.assertEqual(response.status_code,200)
        self.assertEqual(len(state.controller.records),1)
        self.assertEqual(next(iter(state.controller.records.values())).state,'FINISHED')

    async def test_profile_arrives_only_after_native_lookup_and_quota_is_late(self):
        s = AdmissionState(config())
        for rid in ['a','b']:
            future = s.arrive(dict(id=rid,prompt_token_ids=list(range(1024))))
            self.assertTrue(future.done())
            self.assertNotIn(rid, s.controller.records)
            response = resolve(s,snapshot(s.uuid_by_id[rid]))
            self.assertTrue(response['is_heavy_read'])
        self.assertEqual(s.controller.held, 0)
        def acquire(rid):
            return s.control(dict(op='acquire',request_id=s.uuid_by_id[rid],worker_id=0,
                acquisition_key='k',load_attempt_id=1,storage_hint=s.controller.hints[rid]))
        self.assertTrue(acquire('a')['granted'])
        self.assertFalse(acquire('b')['granted'])
        s.local_event('first_token','a')
        self.assertTrue(acquire('b')['granted'])
        s.local_event('first_token','b')
        self.assertEqual(s.controller.held, 0)

    async def test_identical_snapshot_is_idempotent_changed_snapshot_is_rejected(self):
        s = AdmissionState(config())
        s.arrive(dict(id='a',prompt_token_ids=list(range(1024))))
        v = snapshot(s.uuid_by_id['a'])
        first = resolve(s,v)
        self.assertEqual(first, resolve(s,v))
        changed = dict(v, disk_blocks=33)
        with self.assertRaisesRegex(ValueError, 'changed'):
            resolve(s,changed)
        self.assertEqual(len(s.controller.records),1)

    async def test_internal_endpoint_auth_and_no_client_hit_metadata(self):
        app = create_app(config(), 'http://127.0.0.1:9')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            r = await c.post('/internal/dynamo-tier-profile',json={})
            self.assertEqual(r.status_code,403)
            r = await c.post('/v1/completions',json=dict(_sim_admission={
                'id':'x','estimated_remote_tokens':1024}))
            self.assertEqual(r.status_code,400)
            self.assertIn('does not accept',r.text)


if __name__ == '__main__':
    unittest.main()
