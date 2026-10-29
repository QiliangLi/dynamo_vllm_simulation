"""Cross-model invariants: singleton overlap recurrence and concurrent byte integral."""
import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sim.compute_time import estimate_step
from sim.stack.layerwise import layer_durations
from sim.layer_aligned import simulate
from sim.sa_layer_engine import make_sa_request
from sim.request_profile import build_profile,normalize_config
from sim.priority import edf_priority


class LayerAlignedTest(unittest.TestCase):
    def base(self):
        root=Path(__file__).resolve().parents[1]
        cfg=json.loads((root/'configs/mooncake_first_seen_layerwise.json').read_text())
        cfg.update(workers=1,num_blocks=20001,max_model_len=10000,max_num_seqs=4,
            max_num_batched_tokens=8192,router_mode='round-robin')
        cfg['storage'].update(disk_Bps=120e9,shared_link_Bps=120e9,read_latency_s=0.)
        cfg['request_profile']['profile_token_budget']=8192
        cfg['admission'].update(mode='immediate',reference_bandwidth_Bps=120e9)
        return cfg

    def row(self,rid='r0'):
        return dict(id=rid,arrival_s=0.,prompt_token_ids=[3]*6000,hit_tokens=5760,
            u_tokens=240,remote_prefix_tokens=5760,output_tokens=1,class_id='A')

    def test_singleton_matches_independent_overlap_recurrence(self):
        c=self.base();r=self.row()
        durations=layer_durations(estimate_step(c['compute'],[(5760,240)]),c)
        read_s=5760*1408/120e9
        start=read_s
        for layer,duration in enumerate(durations):
            stop=start+duration
            if layer<77:start=max(stop,start+read_s)
        with tempfile.TemporaryDirectory() as out:
            report=asyncio.run(simulate(c,[r],out))
        self.assertAlmostEqual(report['makespan_s'],stop,places=11)
        self.assertEqual(len(report['read_history']),78)

    def test_competing_reads_conserve_bytes_and_bandwidth(self):
        c=self.base();c['workers']=2
        rows=[self.row('r0'),self.row('r1')]
        with tempfile.TemporaryDirectory() as out:
            report=asyncio.run(simulate(c,rows,out))
            events=[json.loads(s) for s in (Path(out)/'events.jsonl').read_text().splitlines()]
        intervals=[e for e in events if e['event']=='storage_interval']
        self.assertTrue(all(e['actual_Bps']<=120e9+1e-3 for e in intervals))
        integral=sum((e['t']-e['from'])*e['actual_Bps'] for e in intervals)
        self.assertAlmostEqual(integral,5760*109824*2,places=3)
        self.assertEqual(report['requests_completed'],2)
        self.assertEqual(len(report['read_history']),156)

    def test_edf_deadline_reaches_the_real_layerwise_request(self):
        c=self.base();c['policy']='priority';row=self.row();created=[]
        def capture(*args,**kwargs):
            request=make_sa_request(*args,**kwargs);created.append(request);return request
        with tempfile.TemporaryDirectory() as out,patch('sim.layer_aligned.make_sa_request',side_effect=capture):
            report=asyncio.run(simulate(c,[row],out))
        self.assertEqual(report['requests_completed'],1)
        self.assertEqual(created[0].priority,edf_priority(build_profile(row,normalize_config(c)).deadline_s))


if __name__=='__main__':unittest.main()
