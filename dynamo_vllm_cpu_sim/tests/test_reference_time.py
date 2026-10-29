"""分模型估时与参考协议的解析验证；不调用真实执行计时作为答案。"""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import unittest

from sim.compute_time import estimate_step,get_compute_time_model
from sim.reference_time import get_reference_time_estimator,SAReferenceTimeEstimator,GLM5ReferenceTimeEstimator
from sim.request_profile import normalize_config,build_profile
from scripts.check_prefill_formula import manual

ROOT=Path(__file__).resolve().parents[1]


def config(mode='glm5_d950pr'):
    if mode=='sa_e26f':return normalize_config(json.loads((ROOT/'configs/sa_abc_stack.json').read_text()))
    c=json.loads((ROOT/'configs/demo.json').read_text())
    c.update(compute=dict(mode=mode,engine_overhead_s=.001),kv_transfer_mode='layer_pipeline',
             enable_prefix_caching=False)
    c['storage'].update(bandwidth_policy='max_min_fair',max_min_read_representation='grouped',
                        kv_bytes_per_token=109824,read_latency_s=.0002)
    c['layerwise']=dict(layers=78,startup='once')
    return normalize_config(c)


def glm_chunk(h,u,overhead=.001):
    t=manual([(h,u)],hit=1.,overhead=overhead)
    layers=[t['dense_layer_us']*1e-6]*3+[t['moe_layer_us']*1e-6]*75
    layers[0]+=overhead
    return t['duration_s'],layers


def pipeline(reads,computes):
    start=reads[0]
    for i in range(len(computes)-1):start+=max(computes[i],reads[i+1])
    return start+computes[-1]


class ReferenceTimeTests(unittest.TestCase):
    def test_models_have_distinct_batch_semantics(self):
        sa=config('sa_e26f');glm=config()
        shapes=[(128,32),(64,16)]
        a=get_compute_time_model(sa['compute']).estimate_step(shapes)
        b=get_compute_time_model(glm['compute']).estimate_step(shapes)
        self.assertEqual(a['padded_prefill_tokens'],48)
        self.assertEqual(b['padded_prefill_tokens'],48)
        self.assertNotEqual(a['duration_s'],b['duration_s'])
        self.assertIsInstance(get_reference_time_estimator(sa),SAReferenceTimeEstimator)
        self.assertIsInstance(get_reference_time_estimator(glm),GLM5ReferenceTimeEstimator)

    def test_sa_reference_matches_exact_singleton_formula_and_qmax(self):
        c=config('sa_e26f');p=c['compute']['profile'];c['admission']['reference_bandwidth_Bps']=300e9
        h,u=1024,256;n=u;pairs=u*h+u*(u+1)//2
        eta=min(Fraction(1),max(Fraction(p['eta_floor']),Fraction(n,p['N_sat'])))
        layer=float(Fraction(p['t_launch_s'])+Fraction(p['a_s_per_token'])*n/eta+Fraction(p['b_s_per_pair'])*pairs)
        x=float(Fraction(p['kappa_gb_per_token_layer']))*1e9*h/min(300e9,c['storage']['q_max_Bps'])
        t=get_reference_time_estimator(c).estimate(h+u,h)
        self.assertAlmostEqual(t.service_s,x+7*max(x,layer)+layer,places=13)

    def test_glm_pipeline_serial_bulk_and_startup(self):
        for mode in ['bulk','layer_serial','layer_pipeline']:
            for startup in ['once','per_layer']:
                c=config();c['kv_transfer_mode']=mode;c['layerwise']['startup']=startup
                total,compute=glm_chunk(1024,512)
                x=1024*1408/c['admission']['reference_bandwidth_Bps']
                reads=[x]*78
                for i in range(78):
                    if i==0 or startup=='per_layer':reads[i]+=.0002
                got=get_reference_time_estimator(c).estimate(1536,1024)
                expected=(total+1024*109824/c['admission']['reference_bandwidth_Bps']+.0002 if mode=='bulk'
                          else total+sum(reads) if mode=='layer_serial' else pipeline(reads,compute))
                self.assertAlmostEqual(got.compute_s,total,places=12)
                self.assertAlmostEqual(got.service_s,expected,places=12)

    def test_chunked_prefix_is_read_once(self):
        c=config();c['request_profile']['profile_token_budget']=128
        t=get_reference_time_estimator(c).estimate(1400,1024)
        first,cs=glm_chunk(1024,128);later=[glm_chunk(1152,128)[0],glm_chunk(1280,120)[0]]
        x=1024*1408/c['admission']['reference_bandwidth_Bps'];reads=[x]*78;reads[0]+=.0002
        self.assertEqual(t.read_bytes,1024*109824)
        self.assertAlmostEqual(t.read_s,1024*109824/c['admission']['reference_bandwidth_Bps']+.0002)
        self.assertAlmostEqual(t.service_s,pipeline(reads,cs)+sum(later),places=12)
        self.assertAlmostEqual(t.compute_s,first+sum(later),places=12)

    def test_zero_read_has_no_startup_and_deadline_uses_selected_provider(self):
        for mode in ['sa_e26f','glm5_d950pr']:
            c=config(mode)
            p=build_profile(dict(id='r',arrival_s=3.,prompt_token_ids=[995]*512,remote_prefix_tokens=0),c)
            self.assertEqual(p.reference_service_s,p.standalone_compute_s)
            self.assertEqual(p.deadline_s,3.+4*p.reference_service_s)

    def test_legacy_profile_mode_migrates_visibly(self):
        for legacy in ('glm5_reference_v1','glm5_reference_v2'):
            c=config();c['request_profile']['mode']=legacy
            with self.assertWarns(FutureWarning):new=normalize_config(c)
            self.assertEqual(new['request_profile']['mode'],'glm5_reference_v3')


if __name__=='__main__':unittest.main()
