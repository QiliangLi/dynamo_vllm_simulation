"""Exercise the patched real Scheduler and real KV allocator on CPU."""
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sim.engine import Engine, make_request
from sim.stack.scheduler_admission import ReadGate, patch_schedule
from sim.stack.storage_hint import attach_http_hint, install_dynamo_adapter

ROOT = Path(__file__).resolve().parents[1]


class SchedulerReadAdmissionTests(unittest.TestCase):
    def engine(self):
        cfg=json.loads((ROOT/'configs/demo.json').read_text())
        cfg.update(scheduler='upstream',enable_prefix_caching=False)
        engine=Engine(0,cfg,SimpleNamespace(present=set()))
        scheduler=engine.scheduler
        client=Mock()
        scheduler._read_gate=ReadGate(client,0)
        scheduler.schedule=MethodType(patch_schedule(type(scheduler).schedule),scheduler)
        return engine,client

    def add(self,engine,rid,token,heavy=True,external=True):
        req=make_request(dict(id=rid,prompt_token_ids=[token]*64,output_tokens=1,arrival_s=0),16,'fcfs')
        req.sampling_params.extra_args=dict(storage_hint=dict(version=1,is_heavy_read=heavy,
            estimated_remote_read_bytes=32*131072,threshold_version=1))
        if external:
            engine.store.present.update(req.block_hashes[:2])
        engine.scheduler.add_request(req)
        return req

    def test_full_preserves_queue_and_schedules_light_after_heavy(self):
        engine,client=self.engine()
        a=self.add(engine,'a',10)
        b=self.add(engine,'b',20)
        c=self.add(engine,'c',30)
        light=self.add(engine,'light',40,False,False)
        client.control.side_effect=[dict(granted=True,permit_id='p'),dict(granted=False,reason='full')]
        scheduler=engine.scheduler
        with patch.object(scheduler.kv_cache_manager,'allocate_slots',wraps=scheduler.kv_cache_manager.allocate_slots) as alloc, \
             patch.object(scheduler.connector,'get_num_new_matched_tokens',wraps=scheduler.connector.get_num_new_matched_tokens) as query:
            output=scheduler.schedule()
        self.assertEqual([c.args[0].request_id for c in alloc.call_args_list],['a','light'])
        self.assertEqual(client.control.call_count,2)
        self.assertIn('light',output.num_scheduled_tokens)
        self.assertEqual({r.request_id for r in scheduler.skipped_waiting},{'a','b','c'})
        self.assertEqual([r[0] for r in scheduler.connector.submissions],['a'])
        self.assertEqual([c.args[0].request_id for c in query.call_args_list],['a','light'])

    def test_rejected_retries_never_query_external_kv(self):
        engine,client=self.engine()
        self.add(engine,'a',10)
        client.control.return_value=dict(granted=False,reason='full')
        with patch.object(engine.scheduler.connector,'get_num_new_matched_tokens') as query:
            for _ in range(2):
                self.assertFalse(engine.scheduler.schedule().num_scheduled_tokens)
        query.assert_not_called()
        self.assertEqual(client.control.call_count,2)

    def test_acquire_precedes_query_and_unknown_hits_roll_back(self):
        engine,client=self.engine()
        self.add(engine,'a',10)
        order=[]
        def control(op,*a,**kw):
            order.append(op)
            return dict(granted=True,permit_id='p')
        def query(*a):
            order.append('query')
            return None,False
        client.control.side_effect=control
        with patch.object(engine.scheduler.connector,'get_num_new_matched_tokens',side_effect=query):
            self.assertFalse(engine.scheduler.schedule().num_scheduled_tokens)
        self.assertEqual(order,['acquire','query','rollback'])
        self.assertFalse(engine.scheduler._read_gate.permits)

    def test_actual_zero_hits_return_early_permit_and_compute_normally(self):
        engine,client=self.engine()
        self.add(engine,'a',10,external=False)
        client.control.return_value=dict(granted=True,permit_id='p')
        output=engine.scheduler.schedule()
        self.assertIn('a',output.num_scheduled_tokens)
        self.assertEqual([c.args[0] for c in client.control.call_args_list],['acquire','rollback'])
        self.assertFalse(engine.scheduler._read_gate.permits)
        self.assertFalse(engine.scheduler.connector.submissions)

    def test_token_budget_exit_returns_early_permit(self):
        engine,client=self.engine()
        self.add(engine,'a',10)
        scheduler=engine.scheduler
        scheduler.max_num_scheduled_tokens=16
        scheduler.scheduler_config.enable_chunked_prefill=False
        with patch.object(scheduler.connector,'get_num_new_matched_tokens',return_value=(32,False)):
            client.control.return_value=dict(granted=True,permit_id='p')
            self.assertFalse(scheduler.schedule().num_scheduled_tokens)
        self.assertEqual([c.args[0] for c in client.control.call_args_list],['acquire','rollback'])
        self.assertFalse(scheduler._read_gate.permits)

    def test_allocation_failure_rolls_back_before_returning(self):
        engine,client=self.engine()
        req=self.add(engine,'a',10)
        client.control.return_value=dict(granted=True,permit_id='p')
        scheduler=engine.scheduler
        with patch.object(scheduler.kv_cache_manager,'allocate_slots',return_value=None):
            output=scheduler.schedule()
        self.assertFalse(output.num_scheduled_tokens)
        self.assertEqual(client.control.call_args.args,('rollback','a'))
        self.assertEqual(client.control.call_args.kwargs,dict(permit_id='p'))
        self.assertEqual([r.request_id for r in scheduler.waiting],['a'])
        self.assertFalse(scheduler.connector.submissions)
        self.assertFalse(scheduler._read_gate.permits)

    def test_real_dynamo_builder_preserves_hint_in_sampling_params(self):
        # This test uses the installed pinned handlers, not a substitute builder.
        import vllm.exceptions as exceptions
        for name in ('VLLMClientError','VLLMUnprocessableEntityError'):
            if not hasattr(exceptions,name):
                setattr(exceptions,name,type(name,(Exception,),{}))
        from dynamo.vllm import handlers
        hint=dict(version=1,is_heavy_read=True,estimated_remote_read_bytes=1024,threshold_version=1)
        body=attach_http_hint({},hint)
        request=dict(annotations=body['nvext']['annotations'],sampling_options={},stop_conditions={'max_tokens':1},
                     extra_args={'custom':123,'kv_transfer_params':{'router_hint':{'worker_id':7}}})
        original=handlers.build_sampling_params
        try:
            install_dynamo_adapter()
            params=handlers.build_sampling_params(request,{'extra_args':{'default_custom':123}},4096)
            self.assertEqual(params.extra_args['storage_hint'],hint)
            self.assertEqual(params.extra_args['default_custom'],123)
            self.assertEqual(params.extra_args['kv_transfer_params']['router_hint'],{'worker_id':7})
            self.assertNotIn('storage_hint',request['extra_args'])
        finally:
            handlers.build_sampling_params=original


if __name__=='__main__':
    unittest.main()
