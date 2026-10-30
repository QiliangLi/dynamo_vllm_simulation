"""Global TFS must retain an unbound heavy request until a real lease release."""
import asyncio,os,sys,time,unittest
from dataclasses import replace
from pathlib import Path

P=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(P),str(P/'tests')]
from sim.stack.admission_service import AdmissionState
from test_stack_admission import config


class TriageTests(unittest.IsolatedAsyncioTestCase):
    async def test_urgent_unbound_candidate_and_release_boundary(self):
        c=config();c.update(policy='priority',request_ordering='edf')
        c['admission'].update(placement='gateway',classification='arrival')
        s=AdmissionState(c)
        for rid,deadline in [('old',10),('urgent',1)]:
            s.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
            rec=s.controller.records[rid];rec.profile=replace(rec.profile,deadline_s=deadline)
        s.admit()
        self.assertEqual(s.controller.cap,1)
        self.assertTrue(s.futures['urgent'].done());self.assertFalse(s.futures['old'].done())
        self.assertEqual(s.controller.records['old'].state,'QUEUED')
        self.assertIsNone(s.controller.records['old'].worker_id)
        self.assertTrue(s.controller.records['urgent'].lease_open)
        s.admit();self.assertFalse(s.futures['old'].done())
        s.local_event('first_token','urgent');s.admit()
        self.assertTrue(s.futures['old'].done());self.assertEqual(s.controller.held,1)
        self.assertFalse(s.controller.permits)


if __name__=='__main__':unittest.main()
