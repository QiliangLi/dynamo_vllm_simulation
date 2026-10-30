"""Idle exception: real native allocation, over-cap ledger and delayed wire."""
import asyncio
import json
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from sim.request_profile import normalize_config
from sim.stack.admission_outbox import AdmissionOutbox
from sim.stack.admission_service import AdmissionState, create_app
from sim.stack.feedback import FeedbackClient
from sim.stack.scheduler_admission import ReadGate
from sim.stack.routing_accounting import ScoreClient
import test_stack_admission as base
import test_scheduler_read_admission as native_tests


def config():
    c=base.config();c.update(scheduler='upstream',kv_transfer_mode='layer_pipeline',enable_prefix_caching=False)
    c['storage']['bandwidth_policy']='max_min_fair'
    c['admission'].update(placement='scheduler',classification='arrival',idle_bypass=True)
    return c


def proof():
    return dict(running_before=0,scheduled_tokens_before=0,nonheavy_candidates=0,
                heavy_candidates=3,allocated=True,external_tokens=32)


class NativeIdleTests(unittest.TestCase):
    engine=native_tests.SchedulerReadAdmissionTests.engine
    add=native_tests.SchedulerReadAdmissionTests.add

    def setup_idle(self):
        engine,client=self.engine()
        client.force_admit_async.side_effect=lambda *a:(uuid.uuid4().hex,time.monotonic())
        engine.scheduler._read_gate=ReadGate(client,idle_bypass=True)
        client.control.return_value=dict(granted=False,reason='full')
        return engine,client

    def test_many_heavy_only_forces_one_after_real_allocation(self):
        engine,client=self.setup_idle();s=engine.scheduler
        for i in range(4):self.add(engine,str(i),10+i)
        order=[];alloc=s.kv_cache_manager.allocate_slots
        def allocate(*a,**kw):order.append('allocation');return alloc(*a,**kw)
        def force(*a):order.append('notice');return uuid.uuid4().hex,time.monotonic()
        client.force_admit_async.side_effect=force
        original_query=s.connector.get_num_new_matched_tokens
        def query(*a):
            tokens,_=original_query(*a)
            return tokens,False
        with patch.object(s.kv_cache_manager,'allocate_slots',side_effect=allocate), \
             patch.object(s.connector,'get_num_new_matched_tokens',side_effect=query):
            out=s.schedule()
        self.assertEqual(order,['allocation','notice'])
        self.assertEqual(list(out.num_scheduled_tokens),['0'])
        self.assertEqual(len(s.skipped_waiting),3)
        client.control.assert_not_called()
        self.assertEqual(client.force_admit_async.call_count,1)

    def test_failed_allocation_and_unknown_hits_never_notify_or_charge(self):
        for stage in ['allocation','query','budget']:
            with self.subTest(stage=stage):
                engine,client=self.setup_idle();s=engine.scheduler;self.add(engine,'a',10)
                if stage=='budget':
                    s.max_num_scheduled_tokens=16;s.scheduler_config.enable_chunked_prefill=False
                with patch.object(s.connector,'get_num_new_matched_tokens',return_value=(None,False) if stage=='query' else (32,False)), \
                     patch.object(s.kv_cache_manager,'allocate_slots',return_value=None):
                    self.assertFalse(s.schedule().num_scheduled_tokens)
                client.force_admit_async.assert_not_called();client.control.assert_not_called()
                self.assertFalse(s._read_gate.permits)

    def test_exhausted_real_block_pool_does_not_create_forced_permit(self):
        engine,client=self.setup_idle();s=engine.scheduler;self.add(engine,'a',10)
        pool=s.kv_cache_manager.block_pool
        held=pool.get_new_blocks(pool.get_num_free_blocks())
        try:
            self.assertFalse(s.schedule().num_scheduled_tokens)
            client.force_admit_async.assert_not_called();client.control.assert_not_called()
            self.assertFalse(s._read_gate.permits)
        finally:pool.free_blocks(held)

    def test_light_candidate_disables_exception_and_still_runs(self):
        engine,client=self.setup_idle();self.add(engine,'a',10);self.add(engine,'light',20,False,False)
        out=engine.scheduler.schedule()
        self.assertIn('light',out.num_scheduled_tokens)
        client.force_admit_async.assert_not_called()

    def test_next_idle_round_forces_without_waiting_for_previous_ledger_release(self):
        from vllm.v1.outputs import ModelRunnerOutput
        engine,client=self.setup_idle();s=engine.scheduler
        self.add(engine,'a',10);self.add(engine,'b',20)
        original_query=s.connector.get_num_new_matched_tokens
        def query(*args):
            tokens,_=original_query(*args)
            return tokens,False
        with patch.object(s.connector,'get_num_new_matched_tokens',side_effect=query):
            first=s.schedule()
            self.assertEqual(list(first.num_scheduled_tokens),['a'])
            s.update_from_output(first,ModelRunnerOutput(req_ids=['a'],req_id_to_index={'a':0},
                sampled_token_ids=[[100]]))
            self.assertFalse(s.running)
            # There is deliberately no central first-token/release confirmation.
            self.assertIn('a',s._read_gate.permits)
            second=s.schedule()
        self.assertEqual(list(second.num_scheduled_tokens),['b'])
        self.assertEqual(client.force_admit_async.call_count,2)
        client.control.assert_not_called()

    def test_existing_compute_or_remote_load_or_partial_prefill_prevents_idle(self):
        from vllm.v1.request import RequestStatus
        for kind in ['running','remote','partial']:
            engine,client=self.setup_idle();s=engine.scheduler;req=self.add(engine,'a',10)
            if kind=='running':
                s.waiting.pop_request();s.running.append(req)
            elif kind=='remote':req.status=RequestStatus.WAITING_FOR_REMOTE_KVS
            else:req.num_computed_tokens=16
            s._read_gate.begin_step(s)
            self.assertIsNone(s._read_gate.force_candidate,kind)


class ForceLedgerTests(unittest.IsolatedAsyncioTestCase):
    arrive=base.StackAdmissionTests.arrive
    raw=base.StackAdmissionTests.raw

    def setup_state(self):
        s=AdmissionState(config())
        for rid in ['a','b','c']:self.arrive(s,rid)
        s.admit();return s

    def force(self,s,rid,worker,token=None,decided=None):
        token=token or uuid.uuid4().hex
        return s.control(dict(op='force_acquire',request_id=s.uuid_by_id[rid],worker_id=worker,
            permit_id=token,acquisition_key='idle:'+token,load_attempt_id=1,
            storage_hint=s.controller.hints[rid],proof=proof(),occurred_mono=time.monotonic() if decided is None else decided))

    async def test_over_cap_is_counted_and_normal_acquire_remains_blocked(self):
        s=self.setup_state();a=self.force(s,'a',0);b=self.force(s,'b',1)
        self.assertEqual(s.controller.held,2);self.assertEqual(s.controller.cap,1)
        normal=dict(op='acquire',request_id=s.uuid_by_id['c'],worker_id=1,acquisition_key='normal',
                    load_attempt_id=1,storage_hint=s.controller.hints['c'])
        self.assertFalse(s.control(normal)['granted'])
        # Duplicate receipt cannot debit a second slot.
        self.force(s,'a',0,a['permit_id']);self.assertEqual(s.controller.held,2)
        s.local_event('first_token','a')
        self.assertEqual(s.controller.held,1);self.assertFalse(s.control(normal)['granted'])
        s.local_event('first_token','b')
        self.assertEqual(s.controller.held,0);self.assertTrue(s.control(normal)['granted'])

    async def test_same_worker_keeps_two_forced_permits_until_individual_first_tokens(self):
        s=self.setup_state();a=self.force(s,'a',0);b=self.force(s,'b',0)
        self.assertNotEqual(a['permit_id'],b['permit_id'])
        self.assertEqual(s.controller.held,2)
        normal=dict(op='acquire',request_id=s.uuid_by_id['c'],worker_id=0,acquisition_key='normal',
                    load_attempt_id=1,storage_hint=s.controller.hints['c'])
        self.assertFalse(s.control(normal)['granted'])
        self.force(s,'a',0,a['permit_id']);self.force(s,'b',0,b['permit_id'])
        self.assertEqual(s.controller.held,2)
        with self.assertRaises(ValueError):self.force(s,'a',0)
        # Release out of registration order. One token cannot release its peer.
        s.local_event('first_token','b');s.local_event('first_token','b')
        self.assertEqual(s.controller.held,1)
        self.assertEqual(s.controller.permits[a['permit_id']]['state'],'reserved')
        self.assertFalse(s.control(normal)['granted'])
        s.local_event('first_token','a')
        self.assertEqual(s.controller.held,0)
        self.assertTrue(s.control(normal)['granted'])
        s.local_event('first_token','c')
        self.assertEqual(s.controller.held,0)

    async def test_gateway_first_token_before_force_notice_keeps_http_timestamp(self):
        s=self.setup_state();decided=time.monotonic()
        s.local_event('first_token','a');s.local_event('request_finished','a')
        actual=s.controller.records['a'].first_token_s
        token=uuid.uuid4().hex
        self.force(s,'a',0,token,decided)
        self.assertEqual(s.controller.records['a'].first_token_s,actual)
        self.assertEqual(s.controller.records['a'].state,'FINISHED')
        self.assertEqual(s.controller.held,0)
        self.assertEqual(s.controller.permits[token]['state'],'released')
        self.force(s,'a',0,token,decided)
        self.assertEqual(sum(e['event']=='admission_released' for e in s.events),1)
        with self.assertRaises(ValueError):
            self.force(s,'a',0,decided=decided)

    async def test_first_token_without_any_external_load_never_waits_for_feedback(self):
        s=self.setup_state()
        s.local_event('first_token','a');s.local_event('request_finished','a')
        self.assertEqual(s.controller.records['a'].state,'FINISHED')
        self.assertEqual(s.controller.held,0)
        self.assertFalse(s.controller.permits)

    async def test_force_decision_after_first_token_is_rejected(self):
        s=self.setup_state();s.local_event('first_token','a')
        with self.assertRaisesRegex(ValueError,'follows'):
            self.force(s,'a',0)
        self.assertEqual(s.controller.held,0)

    async def test_disabled_bad_proof_and_wrong_owner_rejected(self):
        s=self.setup_state();s.controller.c['idle_bypass']=False
        with self.assertRaises(ValueError):self.force(s,'a',0)
        s.controller.c['idle_bypass']=True
        with patch(__name__+'.proof',return_value=dict(proof(),allocated=False)):
            with self.assertRaises(ValueError):self.force(s,'a',0)
        self.assertEqual(s.controller.held,0)
        a=self.force(s,'a',0)
        with self.assertRaises(ValueError):
            s.control(dict(op='rollback',request_id=s.uuid_by_id['a'],worker_id=1,permit_id=a['permit_id']))

    async def test_force_status_operation_is_removed(self):
        s=self.setup_state();a=self.force(s,'a',0)
        with self.assertRaisesRegex(ValueError,'unknown admission operation'):
            s.control(dict(op='force_status',request_id=s.uuid_by_id['a'],worker_id=0,permit_id=a['permit_id']))
        self.assertEqual(s.controller.held,1)


class OutboxTests(unittest.TestCase):
    def test_slow_ack_does_not_block_notice_and_keeps_wire_order(self):
        entered=threading.Event();unblock=threading.Event();seen=[]
        def exchange(row):
            seen.append(row.get('op',row.get('kind')))
            if row.get('op')=='force_acquire':entered.set();unblock.wait(2)
            return dict(ok=True,granted=True,permit_id=row.get('permit_id'))
        client=SimpleNamespace(worker=0,clock_id=None,timeout_s=2,_exchange=exchange)
        outbox=AdmissionOutbox(client)
        try:
            token,_=outbox.force('a',{},proof())
            self.assertTrue(entered.wait(1))
            other,_=outbox.force('b',{},proof())
            self.assertNotEqual(token,other)
            barrier=outbox.send(dict(op='barrier'),wait=False)
            self.assertEqual(seen,['force_acquire'])
            unblock.set()
            barrier.result(timeout=1)
            # The idle queue performs no release-status calls.
            outbox.close()
            self.assertEqual(seen,['force_acquire','force_acquire','barrier'])
        finally:unblock.set();outbox.close()

    def test_ambiguous_error_stops_next_scheduler_call_without_replay(self):
        client=SimpleNamespace(worker=0,clock_id=None,timeout_s=.2,
            _exchange=Mock(side_effect=TimeoutError('ambiguous')))
        outbox=AdmissionOutbox(client)
        try:
            future=outbox.send(dict(op='probe'),wait=False)
            with self.assertRaises(TimeoutError):future.result(timeout=1)
            with self.assertRaises(RuntimeError):outbox.check()
            self.assertEqual(client._exchange.call_count,1)
        finally:outbox.close()

    def test_late_route_activation_only_accepts_confirmed_closed_tombstone(self):
        rid=uuid.uuid4().hex
        for terminal in ['closed','bound_waiting']:
            client=ScoreClient('/unused',.1,1)
            client.sock=Mock();client.identity=dict(epoch='run')
            replies=[dict(ok=False,error='stale activation or request is not waiting'),
                     dict(ok=True,data=dict(epoch='run',events=[dict(id=rid,event=terminal)]))]
            client.reader=io.BytesIO(''.join(json.dumps(r)+'\n' for r in replies).encode())
            try:
                if terminal=='closed':
                    self.assertTrue(client.exchange('activate',rid,permit_id='p',allow_closed=True)['already_closed'])
                    self.assertFalse(client.failed)
                else:
                    with self.assertRaises(RuntimeError):client.exchange('activate',rid,permit_id='p',allow_closed=True)
                    self.assertTrue(client.failed)
            finally:client.close()


class UnixForceTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_progresses_without_load_feedback_while_notice_ack_is_withheld(self):
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def handler(reader,writer):
            try:
                while line:=await reader.readline():
                    row=json.loads(line);seen.append(row)
                    if row.get('op')=='force_acquire':
                        entered.set();await release.wait()
                    writer.write((json.dumps(dict(ok=True,granted=True,permit_id=row.get('permit_id')))+'\n').encode())
                    await writer.drain()
            finally:writer.close();await writer.wait_closed()
        with tempfile.TemporaryDirectory() as tmp:
            server=await asyncio.start_unix_server(handler,tmp+'/control.sock')
            client=FeedbackClient(tmp+'/control.sock',0);client.enable_idle_bypass()
            try:
                client.force_admit_async('a',{},proof());await asyncio.wait_for(entered.wait(),1)
                client.force_admit_async('b',{},proof())
                barrier=client.outbox.send(dict(op='barrier'),wait=False)
                self.assertEqual(len(seen),1)  # No reply yet, caller still progressed.
                release.set()
                await asyncio.wait_for(asyncio.wrap_future(barrier),1)
                await asyncio.to_thread(client.close)
                self.assertEqual([r['op'] for r in seen],['force_acquire','force_acquire','barrier'])
            finally:
                release.set();await asyncio.to_thread(client.close);server.close();await server.wait_closed()

    async def test_real_ordered_socket_same_worker_forces_and_individual_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=create_app(config(),'http://127.0.0.1:1',feedback_endpoint=tmp+'/control.sock')
            async with app.router.lifespan_context(app):
                s=app.state.admission
                for rid in ['a','b']:
                    s.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
                s.admit();client=FeedbackClient(tmp+'/control.sock',0);client.enable_idle_bypass()
                try:
                    for rid in ['a','b']:
                        client.force_admit_async(s.uuid_by_id[rid],s.controller.hints[rid],proof())
                    deadline=time.monotonic()+2
                    while s.controller.held != 2:
                        self.assertLess(time.monotonic(),deadline);await asyncio.sleep(.002)
                    self.assertEqual(s.controller.held,2)
                    s.local_event('first_token','b')
                    self.assertEqual(s.controller.held,1)
                    s.local_event('first_token','a')
                    self.assertEqual(s.controller.held,0)
                finally:
                    await asyncio.to_thread(client.close)


class ConfigTests(unittest.TestCase):
    def test_default_off_and_reject_wrong_mode(self):
        c=config();self.assertTrue(normalize_config(c)['admission']['idle_bypass'])
        c['admission'].pop('idle_bypass');self.assertFalse(normalize_config(c)['admission']['idle_bypass'])
        for field,value in [('idle_bypass',1),('placement','gateway'),('feedback_delay_s',.1)]:
            c=config();c['admission'][field]=value
            with self.assertRaises(ValueError):normalize_config(c)


class PermitAuditTests(unittest.TestCase):
    def test_overlap_and_out_of_order_release_are_counted_without_losing_a_permit(self):
        from scripts.check_read_admission import check_permit_ledger
        permits={'p':dict(id='a',worker=0,forced=True),'q':dict(id='b',worker=0,forced=True)}
        events=[dict(event='admission_reserved',permit_id='p',held_before=0,held_after=1),
                dict(event='admission_reserved',permit_id='q',held_before=1,held_after=2),
                dict(event='admission_released',id='b',held_before=2,held_after=1),
                dict(event='admission_released',id='a',held_before=1,held_after=0)]
        self.assertEqual(check_permit_ledger(events,permits),2)
        for broken in (events[:-1],events+[events[-1]],
                       [events[0],dict(events[1],held_after=1),*events[2:]]):
            with self.assertRaises(AssertionError):check_permit_ledger(broken,permits)


if __name__=='__main__':unittest.main()
