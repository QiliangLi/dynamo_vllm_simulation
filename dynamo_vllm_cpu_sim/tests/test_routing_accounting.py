"""Configuration, late-permit coupling and fail-closed score transport."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from sim.request_profile import normalize_config
from sim.stack.admission_service import AdmissionState
from sim.stack.routing_accounting import ScoreClient
from sim.stack.scheduler_admission import ReadGate
import test_read_admission as late
import test_stack_admission as common


def config(enabled=True):
    c=common.config();c.update(router_mode='kv',scheduler='upstream',block_size=1,enable_prefix_caching=False)
    c['admission'].update(placement='scheduler',classification='arrival')
    c['routing_accounting']=dict(enabled=enabled,waiting_factor=.1)
    return c


class ConfigTests(unittest.TestCase):
    def test_legacy_default_and_factor_boundaries(self):
        self.assertFalse(normalize_config(common.config())['routing_accounting']['enabled'])
        for value in (0,.1,.25,.5,1):
            c=config();c['routing_accounting']['waiting_factor']=value
            self.assertEqual(normalize_config(c)['routing_accounting']['waiting_factor'],value)
        for value in (-.1,1.1,True,float('nan'),float('inf')):
            c=config();c['routing_accounting']['waiting_factor']=value
            with self.assertRaises(ValueError):normalize_config(c)

    def test_unsupported_routing_and_cache_modes_rejected(self):
        for key,value in [('router_mode','round-robin'),('block_size',8),('enable_prefix_caching',True)]:
            c=config();c[key]=value
            with self.assertRaises(ValueError):normalize_config(c)

    def test_block16_keeps_resource_capacity_and_factor(self):
        c=config();c.update(block_size=16,num_blocks=12258)
        normalized=normalize_config(c)
        self.assertEqual(normalized['block_size'],16)
        self.assertEqual(normalized['num_blocks'],12258)
        self.assertEqual(normalized['routing_accounting']['waiting_factor'],.1)


class PermitCouplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_activates_but_quota_denial_does_not(self):
        score=Mock();score.exchange.return_value=dict(duration_wall_ns=1)
        with patch.dict(os.environ,SIM_STACK_ROUTING_SCORE_READY='/unused'),patch('sim.stack.routing_accounting.ScoreClient',return_value=score):
            s=AdmissionState(config())
        for rid in ('a','b'):
            s.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
        s.admit()
        helper=late.LateLedgerTests()
        first=helper.acquire(s,'a');self.assertTrue(first['granted'])
        self.assertEqual(score.exchange.call_args.args[0],'activate')
        score.reset_mock();self.assertFalse(helper.acquire(s,'b',1)['granted']);score.exchange.assert_not_called()
        helper.rollback(s,'a',first['permit_id'])
        self.assertEqual(score.exchange.call_args.args[0],'rollback')
        self.assertEqual(s.controller.held,0)

    async def test_score_failure_prevents_success_reply_without_faking_quota_release(self):
        score=Mock();score.exchange.side_effect=TimeoutError('activation outcome unknown')
        with patch.dict(os.environ,SIM_STACK_ROUTING_SCORE_READY='/unused'),patch('sim.stack.routing_accounting.ScoreClient',return_value=score):
            s=AdmissionState(config())
        s.arrive(dict(id='a',prompt_token_ids=list(range(100)),remote_prefix_tokens=32));s.admit()
        with self.assertRaises(TimeoutError):late.LateLedgerTests().acquire(s,'a')
        self.assertEqual(s.controller.held,1)

    async def test_zero_actual_hit_promotes_to_normal_score_once(self):
        client=Mock();gate=ReadGate(client)
        with patch.dict(os.environ,SIM_STACK_ROUTING_SCORE_READY='/unused'):
            gate.resolved_no_remote(late.request('a'));gate.resolved_no_remote(late.request('a'))
        self.assertEqual(client.control.call_count,1)
        self.assertEqual(client.control.call_args.args,('routing_ready','a'))
        self.assertTrue(gate.allow(late.request('a'),32))
        self.assertEqual(client.control.call_count,1)
        gate.permits['existing']='p'
        with patch.dict(os.environ,SIM_STACK_ROUTING_SCORE_READY='/unused'):
            gate.resolved_no_remote(late.request('existing'))
        self.assertEqual(client.control.call_count,1)


class ScoreTransportTests(unittest.TestCase):
    def test_epoch_factor_persistent_connection_and_ambiguous_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
            identity=Path(tmp)/'ready.json'
            identity.write_text(json.dumps(dict(protocol='waiting-score-v1',factor=.1,epoch='run1',endpoint=f'tcp://127.0.0.1:{listener.getsockname()[1]}')))
            received=[]
            def server():
                conn,_=listener.accept()
                with conn,conn.makefile('rb') as reader:
                    for _ in range(2):
                        received.append(json.loads(reader.readline()))
                        conn.sendall(b'{"ok":true,"data":{"phase":"Active"}}\n')
                    reader.readline() # A lost third reply is not retried.
                listener.close()
            thread=threading.Thread(target=server);thread.start()
            client=ScoreClient(identity,.1,1)
            client.exchange('activate','id',permit_id='p');client.exchange('rollback','id',permit_id='p')
            with self.assertRaises(ConnectionError):client.exchange('activate','id',permit_id='p2')
            self.assertTrue(client.failed)
            self.assertEqual(client.snapshot(),dict(connected=False,failed=True))
            with self.assertRaises(ConnectionError):client.exchange('activate','id',permit_id='p2')
            thread.join(2)
            self.assertEqual([r['epoch'] for r in received],['run1','run1'])
            self.assertEqual(len(received),2)

    def test_wrong_binary_factor_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'ready.json';p.write_text(json.dumps(dict(protocol='waiting-score-v1',factor=1)))
            client=ScoreClient(p,.1,1)
            with self.assertRaises(ValueError):client.exchange('register','id',prompt_tokens=10)


if __name__=='__main__':unittest.main()
