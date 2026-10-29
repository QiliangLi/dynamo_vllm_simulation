"""Late permit safety, wire fidelity and per-round bypass semantics."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sim.stack.admission_service import AdmissionState
from sim.stack.scheduler_admission import ReadGate, patch_schedule
from sim.stack.storage_hint import attach_http_hint, hint_from_request, validate_hint, PREFIX
import test_stack_admission as base_tests


def hint(heavy=True):
    return dict(version=1,is_heavy_read=heavy,estimated_remote_read_bytes=1024,threshold_version=1)


def request(rid, heavy=True):
    return SimpleNamespace(request_id=rid,sampling_params=SimpleNamespace(extra_args={'storage_hint':hint(heavy)}))


class WireTests(unittest.TestCase):
    def test_roundtrip_preserves_other_fields(self):
        body = dict(prompt='hello',nvext=dict(annotations=['other'],other=123))
        encoded = attach_http_hint(body,hint())
        self.assertEqual(hint_from_request(encoded['nvext']),hint())
        self.assertEqual(body['nvext']['annotations'],['other'])
        self.assertEqual(encoded['nvext']['other'],123)
        for annotations in ([],encoded['nvext']['annotations']*2,[PREFIX+'broken']):
            with self.assertRaises(ValueError):
                hint_from_request(dict(annotations=annotations))
        with self.assertRaises(ValueError):
            attach_http_hint(encoded,hint())

    def test_invalid_hint_fails_closed(self):
        for key,value in [('version',True),('is_heavy_read',1),('estimated_remote_read_bytes',-1),('threshold_version',1.5)]:
            with self.assertRaises(ValueError):
                validate_hint({**hint(),key:value})
        with self.assertRaises(ValueError):
            validate_hint({**hint(),'surprise':1})


class LateLedgerTests(unittest.IsolatedAsyncioTestCase):
    arrive = base_tests.StackAdmissionTests.arrive
    raw = base_tests.StackAdmissionTests.raw

    def state(self):
        c = base_tests.config()
        c['scheduler'] = 'upstream'
        c['admission'].update(placement='scheduler',classification='arrival')
        s = AdmissionState(c)
        for rid in ('a','b'):
            self.arrive(s,rid)
        s.admit()
        return s

    def acquire(self,s,rid,worker=0,key='first'):
        return s.control(dict(op='acquire',request_id=s.uuid_by_id[rid]+'_0',worker_id=worker,
                              acquisition_key=key,load_attempt_id=1,storage_hint=s.controller.hints[rid]))

    def rollback(self,s,rid,token,worker=0):
        return s.control(dict(op='rollback',request_id=s.uuid_by_id[rid],worker_id=worker,permit_id=token))

    async def test_forward_without_reservation_and_global_contention(self):
        s = self.state()
        self.assertTrue(all(f.done() for f in s.futures.values()))
        self.assertEqual(s.controller.held,0)
        a = self.acquire(s,'a')
        self.assertTrue(a['granted'])
        self.assertEqual(self.acquire(s,'a')['permit_id'],a['permit_id'])
        self.assertEqual(self.acquire(s,'b',1)['reason'],'full')
        self.assertEqual(s.controller.held,1)
        s.receive(self.raw(s,'a','load_submitted',payload=dict(attempt_id=1,submitted_read_bytes=1024),worker=0))
        s.receive(self.raw(s,'a','load_completed','e2',dict(attempt_id=1),worker=0))
        self.assertEqual(self.acquire(s,'b',1)['reason'],'full')
        s.local_event('first_token','a')
        self.assertTrue(self.acquire(s,'b',1)['granted'])
        self.assertEqual(s.controller.permits[a['permit_id']]['state'],'released')

    async def test_rollback_idempotency_and_new_attempt_key(self):
        s = self.state()
        a = self.acquire(s,'a')
        with self.assertRaises(ValueError):
            self.rollback(s,'a',a['permit_id'],1)
        self.rollback(s,'a',a['permit_id'])
        self.rollback(s,'a',a['permit_id'])
        self.assertEqual(s.controller.held,0)
        self.assertFalse(self.acquire(s,'a')['granted'])
        retry = self.acquire(s,'a',key='retry')
        self.assertNotEqual(retry['permit_id'],a['permit_id'])
        s.receive(self.raw(s,'a','load_submitted',payload=dict(attempt_id=1,submitted_read_bytes=1024),worker=0))
        with self.assertRaises(ValueError):
            self.rollback(s,'a',retry['permit_id'])

    async def test_unpermitted_read_and_bad_metadata_fail(self):
        s = self.state()
        with self.assertRaises(ValueError):
            s.receive(self.raw(s,'a','load_submitted',payload=dict(attempt_id=1,submitted_read_bytes=1024),worker=0))
        with self.assertRaises(ValueError):
            s.controller.acquire('b',1,'k',1,hint(),s.now())

    async def test_classification_is_frozen_and_faults_stop_acquire(self):
        s = self.state()
        original = dict(s.controller.hints['a'])
        for i in range(8):
            s.arrive(dict(id=f'cold{i}',prompt_token_ids=list(range(100)),remote_prefix_tokens=0))
        s.admit()
        self.assertEqual(s.controller.hints['a'],original)
        self.assertFalse(s.controller.hints['cold0']['is_heavy_read'])
        s.fatal = 'transport uncertain'
        with self.assertRaises(RuntimeError):
            self.acquire(s,'a')

    async def test_cap_shrink_does_not_revoke_existing_permits(self):
        s = self.state()
        s.controller.c['reference_bandwidth_Bps'] = 2*s.controller.records['a'].profile.pressure_Bps
        s.controller.cap = 2
        self.acquire(s,'a',0)
        self.acquire(s,'b',1)
        for rid in ('c','d'):
            s.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=96))
        s.admit()
        self.assertEqual(s.controller.cap,1)
        self.assertEqual(s.controller.held,2)
        self.assertTrue(s.controller.is_heavy(s.controller.records['a'].profile))
        self.assertEqual(self.acquire(s,'c',0)['reason'],'full')
        s.local_event('first_token','a')
        self.assertEqual(self.acquire(s,'c',0)['reason'],'full')
        s.local_event('first_token','b')
        self.assertTrue(self.acquire(s,'c',0)['granted'])


class GateTests(unittest.TestCase):
    def test_full_skips_other_heavy_but_light_and_existing_permit_pass(self):
        client = Mock()
        client.control.side_effect = [dict(granted=True,permit_id='p'),dict(granted=False,reason='full')]
        gate = ReadGate(client,retry_interval=0)
        gate.begin_step()
        self.assertTrue(gate.allow(request('a'),32))
        self.assertFalse(gate.allow(request('b'),32))
        self.assertFalse(gate.allow(request('c'),32))
        self.assertTrue(gate.allow(request('light',False),32))
        self.assertTrue(gate.allow(request('no_io'),0))
        self.assertTrue(gate.allow(request('a'),32))
        self.assertEqual(client.control.call_count,2)
        gate.begin_step()
        client.control.side_effect = None
        client.control.return_value = dict(granted=True,permit_id='q')
        self.assertTrue(gate.allow(request('b'),32))

    def test_rollback_only_owned_permit_and_new_key(self):
        client = Mock()
        client.control.return_value = dict(granted=True,permit_id='p')
        gate = ReadGate(client)
        gate.rollback(request('light',False))
        client.control.assert_not_called()
        gate.allow(request('a'),1)
        gate.rollback(request('a'))
        self.assertNotIn('a',gate.permits)
        gate.allow(request('a'),1)
        self.assertEqual(client.control.call_args.kwargs['acquisition_key'],'a:allocation:1')

    def test_transport_error_is_not_quota_rejection(self):
        client = Mock()
        client.control.side_effect = TimeoutError('unknown result')
        gate = ReadGate(client)
        with self.assertRaises(TimeoutError):
            gate.allow(request('a'),1)
        self.assertFalse(gate.full)
        self.assertFalse(gate.permits)

    def test_later_allocation_failure_cannot_release_existing_io(self):
        client = Mock()
        client.control.return_value = dict(granted=True,permit_id='p')
        gate = ReadGate(client)
        gate.begin_step()
        gate.allow(request('a'),32)
        gate.begin_step()
        self.assertTrue(gate.allow(request('a'),0))
        gate.rollback(request('a'))
        self.assertEqual(client.control.call_count,1)
        self.assertEqual(gate.permits['a'],'p')

    def test_unknown_scheduler_source_rejected(self):
        with self.assertRaises(RuntimeError):
            patch_schedule(self.test_unknown_scheduler_source_rejected)


if __name__ == '__main__':
    unittest.main()
