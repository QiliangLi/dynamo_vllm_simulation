"""Finite-window input normalization and config-dependent reference checks."""
from fractions import Fraction as F
import json
from pathlib import Path
import unittest

from scripts.run_sa_pressure import scale_rows
from scripts.check_sa_results import check_profile

PROFILE=json.loads((Path(__file__).resolve().parents[1]/"configs/sa_e26f_profile.json").read_text())


def reference_fraction(h,u,bandwidth,qmax):
    eta=min(F(1),max(F(PROFILE['eta_floor']),F(u,PROFILE['N_sat'])))
    c=F(PROFILE['t_launch_s'])+F(PROFILE['a_s_per_token'])*u/eta+F(PROFILE['b_s_per_pair'])*(h*u+u*(u+1)//2)
    v=F(h*4096);read=v/min(F(bandwidth),F(qmax))
    return float(read+8*c+7*max(F(0),read-c)),float(v/c)


class SAPressureTests(unittest.TestCase):
    def test_scaled_arrivals_preserve_ties_shapes_and_recompute_deadlines(self):
        rows=[dict(id='a',arrival_s=1.,class_id='A',h_tokens=42883,u_tokens=256,T0_s=-1,deadline_s=-1),
              dict(id='b',arrival_s=1.,class_id='B',h_tokens=9399,u_tokens=4096,T0_s=-1,deadline_s=-1),
              dict(id='c',arrival_s=2.,class_id='C',h_tokens=0,u_tokens=2657,T0_s=-1,deadline_s=-1)]
        output,info=scale_rows(rows,PROFILE,60e9,.8,200e9)
        self.assertEqual(output[0]['arrival_s'],output[1]['arrival_s'])
        self.assertAlmostEqual(max(r['arrival_s'] for r in output),info['planned_window_s'])
        self.assertAlmostEqual(info['total_read_bytes']/info['planned_window_s']/60e9,.8)
        for before,after in zip(rows,output):
            self.assertEqual([(before[k],after[k]) for k in ('id','class_id','h_tokens','u_tokens')],
                             [(before[k],before[k]) for k in ('id','class_id','h_tokens','u_tokens')])
            expected,_=reference_fraction(after['h_tokens'],after['u_tokens'],60e9,200e9)
            self.assertAlmostEqual(after['T0_s'],expected,places=14)
            self.assertAlmostEqual(after['deadline_s'],after['arrival_s']+4*expected,places=14)
        self.assertEqual(rows[0]['T0_s'],-1)

    def test_checker_honors_bandwidth_and_qmax_and_rejects_old_reference(self):
        for bandwidth,qmax in [(60e9,200e9),(60e9,20e9),(120e9,200e9)]:
            t0,pressure=reference_fraction(42883,256,bandwidth,qmax)
            row=dict(h_tokens=42883,u_tokens=256,T0_s=t0)
            config=dict(sa_profile=PROFILE,storage=dict(shared_link_Bps=bandwidth,q_max_Bps=qmax))
            check_profile(row,dict(pressure_Bps=pressure),config)
        wrong,_=reference_fraction(42883,256,120e9,200e9)
        with self.assertRaises(AssertionError):
            check_profile(dict(h_tokens=42883,u_tokens=256,T0_s=wrong),dict(pressure_Bps=pressure),
                          dict(sa_profile=PROFILE,storage=dict(shared_link_Bps=60e9,q_max_Bps=200e9)))

    def test_invalid_normalization_is_rejected(self):
        for rows,B,rho,q in [([],60e9,.8,200e9),([dict(h_tokens=1,arrival_s=1)],0,.8,200e9),
                             ([dict(h_tokens=1,arrival_s=1)],60e9,1.1,200e9),
                             ([dict(h_tokens=0,arrival_s=1)],60e9,.8,200e9),
                             ([dict(h_tokens=1,arrival_s=0)],60e9,.8,200e9)]:
            with self.assertRaises(ValueError):scale_rows(rows,PROFILE,B,rho,q)


if __name__=='__main__':unittest.main()
