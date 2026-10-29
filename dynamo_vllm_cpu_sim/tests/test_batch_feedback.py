"""Transport safety checks against the patched frozen copy, including real TCP."""
import asyncio,json,os,sys,tempfile,time,unittest
from pathlib import Path

P=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(P),str(P/'tests')]
from sim.stack.admission_service import create_app
from sim.stack.feedback import FeedbackClient
from test_stack_admission import config


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();ready=Path(self.tmp.name)/'ready.json'
        c=config();c['admission'].update(mode='immediate',reference_bandwidth_Bps=1e12)
        c['experiment']=dict(batch_feedback=True)
        self.app=create_app(c,'http://127.0.0.1:1',feedback_endpoint='tcp://127.0.0.1:0',feedback_ready_file=ready)
        self.lifespan=self.app.router.lifespan_context(self.app);await self.lifespan.__aenter__()
        self.state=self.app.state.admission
        self.client=FeedbackClient(json.loads(ready.read_text())['endpoint'],0)
        self.ids=[]
        for rid in ['a','b']:
            self.state.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
        self.state.admit()
        for rid in ['a','b']:
            engine=self.state.uuid_by_id[rid];self.ids.append(engine)
            await asyncio.to_thread(self.client.submitted,engine,0,layer_count=3,expected_read_bytes=96)

    async def asyncTearDown(self):
        self.client.close();await self.lifespan.__aexit__(None,None,None);self.tmp.cleanup()

    async def test_real_tcp_conserves_facts_and_release_boundary(self):
        for layer in range(3):
            response=await asyncio.to_thread(self.client.layer_batch,self.ids,layer,[32,32])
            self.assertEqual(response['events'],2)
            for rid in ['a','b']:
                self.assertEqual(self.state.controller.records[rid].submitted_read_bytes,32*(layer+1))
                self.assertTrue(self.state.controller.records[rid].lease_open)
        for engine in self.ids:await asyncio.to_thread(self.client.completed,engine)
        self.assertEqual(len(self.state.controller.seen_layers),6)
        for rid in ['a','b']:
            self.assertTrue(self.state.controller.records[rid].lease_open)
            self.state.local_event('first_token',rid)
        self.assertEqual(self.state.controller.held,0)
        facts=[e for e in self.state.events if e['event']=='feedback_received' and e['kind']=='layer_load_submitted']
        self.assertEqual(len(facts),6);self.assertEqual(len({e['event_id'] for e in facts}),6)
        self.assertEqual([e['payload']['layer'] for e in facts],[0,0,1,1,2,2])

    async def test_mixed_layer_fails_run_closed(self):
        rows=[dict(event_id=f'bad-{i}',kind='layer_load_submitted',request_id=engine,
              worker_id=0,occurred_mono=time.monotonic(),payload=dict(attempt_id=1,layer=i,submitted_read_bytes=32))
              for i,engine in enumerate(self.ids)]
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(self.client._exchange,dict(op='layer_feedback_batch',rows=rows))
        self.assertTrue(self.state.fatal);self.assertTrue(self.client.closed)
        self.assertFalse(self.state.controller.seen_layers)

    async def test_duplicate_request_fails_before_mutation(self):
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(self.client.layer_batch,[self.ids[0]]*2,0,[32,32])
        self.assertTrue(self.state.fatal);self.assertFalse(self.state.controller.seen_layers)


if __name__=='__main__':unittest.main()
