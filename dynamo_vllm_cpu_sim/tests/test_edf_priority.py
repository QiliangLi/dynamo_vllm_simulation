"""EDF 固定期限、时钟不变性、兼容迁移与真实原生队列。"""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from sim.engine import Engine,make_request
from sim.sa_layer_engine import make_sa_request
from sim.priority import edf_priority,normalize_ordering
from sim.stack.edf_priority import attach_http,calculate,enabled,from_annotations,refresh,validate

ROOT=Path(__file__).resolve().parents[1]


class EdfPriorityTests(unittest.TestCase):
    def value(self,deadline=1.3,T0=.25,arrival=.3):
        return dict(version=1,arrival_s=arrival,deadline_s=deadline,reference_service_s=T0,
                    origin_monotonic_s=100.,wall_time_scale=10.)

    def test_fixed_deadline_independent_of_now_T0_and_scale(self):
        value=self.value()
        for now in [101.,120.,200.]:
            self.assertEqual(calculate(value,now)[1:],(1.3,1300000000))
        self.assertEqual(calculate({**value,'reference_service_s':.01,'wall_time_scale':1.},101.)[2],1300000000)

    def test_metadata_roundtrip_and_reserved_names(self):
        v=self.value();p=SimpleNamespace(arrival_s=v['arrival_s'],deadline_s=v['deadline_s'],reference_service_s=v['reference_service_s'])
        body,meta=attach_http({'nvext':{'annotations':['other']}},p,100.,10.)
        self.assertEqual(from_annotations({'annotations':body['nvext']['annotations']}),meta)
        self.assertEqual(body['nvext']['annotations'][0],'other')
        for prefix in ['edf_priority_v1:','budget_priority_v1:']:
            with self.assertRaises(ValueError):attach_http({'nvext':{'annotations':[prefix+'x']}},p,100.,10.)

    def test_invalid_metadata_and_deadlines(self):
        for field in ['deadline_s','reference_service_s','origin_monotonic_s','wall_time_scale']:
            with self.assertRaises(ValueError):validate({**self.value(),field:float('nan')})
        for value in [-1,float('inf'),True,1e20]:
            with self.assertRaises(ValueError):edf_priority(value)
        with self.assertRaises(ValueError):from_annotations({})

    def test_all_priority_is_edf_and_legacy_label_is_explicit(self):
        self.assertFalse(enabled({}))
        self.assertTrue(enabled({'policy':'priority'}))
        c={'policy':'priority','request_ordering':'budget_priority'}
        with self.assertWarns(FutureWarning):normalize_ordering(c)
        self.assertEqual(c['request_ordering'],'edf')
        for c in [dict(policy='fcfs',request_ordering='edf'),dict(policy='priority',request_ordering='fcfs')]:
            with self.assertRaises(ValueError):normalize_ordering(c)

    def test_large_times_use_64_bit_key(self):
        self.assertEqual(edf_priority(3600.),3600000000000)
        self.assertEqual(edf_priority(1e6),1000000000000000)

    def test_real_heap_selects_edf_when_slack_would_select_other_request(self):
        c=json.loads((ROOT/'configs/demo.json').read_text())
        c.update(scheduler='upstream',policy='priority',enable_prefix_caching=False,max_num_seqs=1)
        e=Engine(0,c,SimpleNamespace(present=set()))
        refs={}
        for rid,deadline,T0,arrival,token in [('A',1.3,.25,.3,10),('B',1.2,.05,1.,20)]:
            v=self.value(deadline,T0,arrival)
            r=make_request(dict(id=rid,deadline_s=deadline,prompt_token_ids=[token]*32,
                output_tokens=1,arrival_s=arrival),16,'priority')
            r.sampling_params.extra_args={'edf_priority':v}
            e.scheduler.add_request(r);refs[rid]=r
        # At model time 1.0, slack would choose A: .05 < .15. EDF chooses B.
        rows,heads=refresh(e.scheduler,110.)
        self.assertEqual(heads['waiting'],'B')
        self.assertEqual([r.request_id for r in e.scheduler.waiting],['B','A'])
        keys={r.request_id:r.priority for r in e.scheduler.waiting}
        refresh(e.scheduler,200.)  # Both deadlines passed; keys remain fixed.
        self.assertEqual(keys,{r.request_id:r.priority for r in e.scheduler.waiting})
        out=e.scheduler.schedule()
        self.assertEqual(list(out.num_scheduled_tokens),['B'])

    def test_des_priority_requires_deadline_and_ignores_arbitrary_trace_key(self):
        row=dict(id='r',prompt_token_ids=[10]*32,output_tokens=1,arrival_s=0.,priority=-99)
        with self.assertRaisesRegex(ValueError,'deadline'):make_request(row,16,'priority')
        self.assertEqual(make_request({**row,'deadline_s':2.},16,'priority').priority,2000000000)

    def test_layerwise_request_uses_the_same_edf_key(self):
        row=dict(id='layer',prompt_token_ids=[10]*32,arrival_s=.5,deadline_s=2.)
        self.assertEqual(make_sa_request(row,'priority').priority,edf_priority(2.))
        self.assertEqual(make_sa_request(row,'fcfs').priority,0)


if __name__=='__main__':unittest.main()
