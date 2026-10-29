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
        client.outbox.busy.return_value=False
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

    def test_unreleased_exception_disables_next_force(self):
        engine,client=self.setup_idle();self.add(engine,'a',10)
        client.outbox.busy.return_value=True
        self.assertFalse(engine.scheduler.schedule().num_scheduled_tokens)
        client.force_admit_async.assert_not_called();client.control.assert_called_once()

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

    def force(self,s,rid,worker,token=None):
        token=token or uuid.uuid4().hex
        return s.control(dict(op='force_acquire',request_id=s.uuid_by_id[rid],worker_id=worker,
            permit_id=token,acquisition_key='idle:'+token,load_attempt_id=1,
            storage_hint=s.controller.hints[rid],proof=proof(),occurred_mono=time.monotonic()))

    async def test_over_cap_is_counted_and_normal_acquire_remains_blocked(self):
        s=self.setup_state();a=self.force(s,'a',0);b=self.force(s,'b',1)
        self.assertEqual(s.controller.held,2);self.assertEqual(s.controller.cap,1)
        normal=dict(op='acquire',request_id=s.uuid_by_id['c'],worker_id=1,acquisition_key='normal',
                    load_attempt_id=1,storage_hint=s.controller.hints['c'])
        self.assertFalse(s.control(normal)['granted'])
        with self.assertRaises(ValueError):self.force(s,'c',1)
        # Duplicate receipt cannot debit a second slot.
        self.force(s,'a',0,a['permit_id']);self.assertEqual(s.controller.held,2)
        s.local_event('first_token','a')  # Deferred until its engine facts arrive.
        s.receive(self.raw(s,'a','load_submitted',payload=dict(attempt_id=1,submitted_read_bytes=1024),worker=0))
        s.receive(self.raw(s,'a','load_completed','done-a',dict(attempt_id=1),worker=0))
        self.assertEqual(s.controller.held,1);self.assertFalse(s.control(normal)['granted'])
        s.local_event('first_token','b')
        s.receive(self.raw(s,'b','load_submitted','submit-b',payload=dict(attempt_id=1,submitted_read_bytes=1024),worker=1))
        s.receive(self.raw(s,'b','load_completed','done-b',dict(attempt_id=1),worker=1))
        self.assertEqual(s.controller.held,0);self.assertTrue(s.control(normal)['granted'])

    async def test_gateway_first_token_before_force_notice_keeps_http_timestamp(self):
        s=self.setup_state();s.local_event('first_token','a');s.local_event('request_finished','a')
        actual=next(e['occurred_s'] for e in s.events if e['event']=='gateway_event_deferred')
        self.assertIsNone(s.controller.records['a'].first_token_s)
        self.force(s,'a',0)
        s.receive(self.raw(s,'a','load_submitted',payload=dict(attempt_id=1,submitted_read_bytes=1024),worker=0))
        s.receive(self.raw(s,'a','load_completed','done',dict(attempt_id=1),worker=0))
        self.assertEqual(s.controller.records['a'].first_token_s,actual)
        self.assertEqual(s.controller.records['a'].state,'FINISHED')
        self.assertEqual(s.controller.held,0);self.assertFalse(s.pending_engine_events)

    async def test_disabled_bad_proof_and_wrong_owner_rejected(self):
        s=self.setup_state();s.controller.c['idle_bypass']=False
        with self.assertRaises(ValueError):self.force(s,'a',0)
        s.controller.c['idle_bypass']=True
        with patch(__name__+'.proof',return_value=dict(proof(),allocated=False)):
            with self.assertRaises(ValueError):self.force(s,'a',0)
        self.assertEqual(s.controller.held,0)
        a=self.force(s,'a',0)
        with self.assertRaises(ValueError):
            s.control(dict(op='force_status',request_id=s.uuid_by_id['a'],worker_id=1,permit_id=a['permit_id']))


class OutboxTests(unittest.TestCase):
    def test_slow_ack_does_not_block_notice_and_keeps_wire_order(self):
        entered=threading.Event();unblock=threading.Event();seen=[]
        def exchange(row):
            seen.append(row.get('op',row.get('kind')))
            if row.get('op')=='force_acquire':entered.set();unblock.wait(2)
            return dict(ok=True,granted=True,permit_id=row.get('permit_id'),released=True)
        client=SimpleNamespace(worker=0,clock_id=None,timeout_s=2,_exchange=exchange)
        outbox=AdmissionOutbox(client)
        try:
            token,_=outbox.force('a',{},proof())
            self.assertTrue(entered.wait(1))
            # While the notification still waits on its ack, the caller can
            # enqueue IO facts and return. No completion/permission is awaited.
            future=outbox.send(dict(kind='load_submitted',request_id='a'),wait=False)
            self.assertFalse(future.done());self.assertTrue(outbox.busy())
            unblock.set();future.result(timeout=1)
            self.assertEqual(seen[:2],['force_acquire','load_submitted'])
            outbox.send(dict(kind='load_completed',request_id='a'),wait=False).result(timeout=1)
            deadline=time.monotonic()+1
            while outbox.busy() and time.monotonic()<deadline:time.sleep(.002)
            self.assertFalse(outbox.busy())
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
    async def test_client_feedback_returns_while_notice_ack_is_withheld(self):
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def handler(reader,writer):
            try:
                while line:=await reader.readline():
                    row=json.loads(line);seen.append(row)
                    if row.get('op')=='force_acquire':
                        entered.set();await release.wait()
                    writer.write((json.dumps(dict(ok=True,granted=True,permit_id=row.get('permit_id'),released=True))+'\n').encode())
                    await writer.drain()
            finally:writer.close();await writer.wait_closed()
        with tempfile.TemporaryDirectory() as tmp:
            server=await asyncio.start_unix_server(handler,tmp+'/control.sock')
            client=FeedbackClient(tmp+'/control.sock',0);client.enable_idle_bypass()
            try:
                client.force_admit_async('a',{},proof());await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(asyncio.to_thread(client.submitted,'a',1024),.25)
                self.assertEqual(len(seen),1)  # No reply yet, caller still progressed.
                release.set()
                deadline=time.monotonic()+1
                while len(seen)<2:
                    self.assertLess(time.monotonic(),deadline);await asyncio.sleep(.002)
                self.assertEqual(seen[1]['kind'],'load_submitted')
            finally:
                release.set();await asyncio.to_thread(client.close);server.close();await server.wait_closed()

    async def test_real_ordered_socket_notice_reads_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=create_app(config(),'http://127.0.0.1:1',feedback_endpoint=tmp+'/control.sock')
            async with app.router.lifespan_context(app):
                s=app.state.admission
                for rid in ['a','b']:
                    s.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
                s.admit();clients=[FeedbackClient(tmp+'/control.sock',i) for i in range(2)]
                try:
                    for client,rid in zip(clients,['a','b']):
                        client.enable_idle_bypass();engine=s.uuid_by_id[rid]
                        client.force_admit_async(engine,s.controller.hints[rid],proof())
                        client.submitted(engine,1024);client.completed(engine)
                    deadline=time.monotonic()+2
                    while any(r.load_done_s is None for r in s.controller.records.values()):
                        self.assertLess(time.monotonic(),deadline);await asyncio.sleep(.002)
                    self.assertEqual(s.controller.held,2)
                    for rid in ['a','b']:s.local_event('first_token',rid)
                    while any(client.outbox.busy() for client in clients):
                        self.assertLess(time.monotonic(),deadline);await asyncio.sleep(.002)
                    self.assertEqual(s.controller.held,0)
                finally:
                    await asyncio.gather(*(asyncio.to_thread(c.close) for c in clients))


class ConfigTests(unittest.TestCase):
    def test_default_off_and_reject_wrong_mode(self):
        c=config();self.assertTrue(normalize_config(c)['admission']['idle_bypass'])
        c['admission'].pop('idle_bypass');self.assertFalse(normalize_config(c)['admission']['idle_bypass'])
        for field,value in [('idle_bypass',1),('placement','gateway'),('feedback_delay_s',.1)]:
            c=config();c['admission'][field]=value
            with self.assertRaises(ValueError):normalize_config(c)


if __name__=='__main__':unittest.main()
