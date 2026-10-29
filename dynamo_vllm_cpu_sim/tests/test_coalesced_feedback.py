"""The completion barrier must conserve all buffered facts or fail the run."""
import asyncio,os,unittest
from pathlib import Path

import test_batch_feedback as batch_fixtures


class CoalescedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await batch_fixtures.BatchTests.asyncSetUp(self)
        self.state.config.update(kv_transfer_mode='layer_pipeline',enable_prefix_caching=False,
            layerwise=dict(layers=3,startup='once'))
        self.state.config['compute']['mode']='linear'
        self.state.config['storage'].update(bandwidth_policy='max_min_fair',
            max_min_read_representation='grouped',kv_bytes_per_token=300)
        self.state.config['experiment']['coalesced_feedback']=True
        self.client.enable_layer_buffer(6)

    async def asyncTearDown(self):await batch_fixtures.BatchTests.asyncTearDown(self)

    async def test_acknowledged_flush_conserves_facts_before_completion(self):
        calls=[];exchange=self.client._exchange
        def recorded(row):calls.append(row.get('op',row.get('kind')));return exchange(row)
        self.client._exchange=recorded
        for layer in range(3):
            response=await asyncio.to_thread(self.client.layer_batch,self.ids,layer,[32,32])
            self.assertTrue(response['buffered'])
        facts=list(self.client.pending_layer_facts)
        self.assertEqual(calls,[])
        self.assertEqual(self.state.controller.records['a'].submitted_read_bytes,0)
        await asyncio.to_thread(self.client.completed,self.ids[0])
        self.assertEqual(calls,['layer_feedback_flush','load_completed'])
        self.assertEqual(self.client.pending_layer_facts,[])
        self.assertEqual(len(self.state.controller.seen_layers),6)
        received=[e for e in self.state.events if e['event']=='feedback_received' and e['kind']=='layer_load_submitted']
        self.assertEqual([e['event_id'] for e in received],[e['event_id'] for e in facts])
        self.assertEqual([e['sender_occurred_mono'] for e in received],[e['occurred_mono'] for e in facts])
        for rid in ['a','b']:
            self.assertEqual(self.state.controller.records[rid].submitted_read_bytes,96)
            self.assertTrue(self.state.controller.records[rid].lease_open)
        self.state.local_event('first_token','a')
        self.assertFalse(self.state.controller.records['a'].lease_open)

    async def test_rejected_flush_does_not_complete_or_release(self):
        for layer in range(3):
            await asyncio.to_thread(self.client.layer_batch,self.ids,layer,[31 if layer==2 else 32,32])
        with self.assertRaises((RuntimeError,ConnectionError)):
            await asyncio.to_thread(self.client.completed,self.ids[0])
        rec=self.state.controller.records['a']
        self.assertIsNone(rec.load_done_s);self.assertIsNone(rec.first_token_s)
        self.assertTrue(rec.lease_open);self.assertIsNotNone(self.state.fatal)

    async def test_buffer_bound_fails_without_sending_or_evicting_facts(self):
        for layer in range(3):await asyncio.to_thread(self.client.layer_batch,self.ids,layer,[32,32])
        original=list(self.client.pending_layer_facts)
        with self.assertRaisesRegex(RuntimeError,'bound exceeded'):
            await asyncio.to_thread(self.client.layer_batch,self.ids,3,[32,32])
        self.assertEqual(self.client.pending_layer_facts,original)
        self.assertFalse(self.state.controller.seen_layers)


if __name__=='__main__':unittest.main()
