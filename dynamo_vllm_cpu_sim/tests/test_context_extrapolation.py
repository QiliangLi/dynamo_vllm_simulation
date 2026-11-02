"""Opt-in context limits preserve frozen math and default rejection boundaries."""
import copy
import math
import unittest

from sim.compute_time import estimate_step, validate_compute
from sim.grouped_storage import GroupedStorage
from sim.timing_models import glm5_d950pr_reference as ref
from sim.timing_models.prefill_time_batch import estimate_batch
from scripts.check_prefill_formula import manual_timing


class ContextExtrapolationTests(unittest.TestCase):
    def config(self, **changes):
        return dict(mode='glm5_d950pr',batch_time_model='glm5_direct_sum_v1',
                    launch_saving_fraction=1.,tp=1,dp=8,ep=8,**changes)

    def test_default_rejects_long_context_after_opt_in_call(self):
        before=copy.deepcopy(ref.MODEL)
        c=self.config(context_length_limit=300481)
        for n in (131072,131073,300480,300481):
            result=estimate_step(c,[(n-16,16)])
            manual=manual_timing(result)
            self.assertTrue(math.isclose(result['duration_s'],manual['duration_s'],abs_tol=1e-12))
            self.assertEqual(result['formula_context_extrapolation'],n>131072)
        with self.assertRaises(ValueError):estimate_step(self.config(),[(300464,16)])
        with self.assertRaises(ValueError):estimate_step(c,[(300481,1)])
        self.assertEqual(ref.MODEL,before)

    def test_short_context_math_is_identical_with_explicit_limit(self):
        for mode in ('glm5_actual_batch_work_v2','glm5_direct_sum_v1'):
            opts=dict(tp=1,dp=8,ep=8,batch_time_model=mode)
            a=estimate_batch([(4096,512),(512,512)],**opts)
            b=estimate_batch([(4096,512),(512,512)],context_length_limit=300481,**opts)
            self.assertEqual(a,b)

    def test_long_mixed_batch_matches_independent_formula(self):
        result=estimate_step(self.config(context_length_limit=300481),[(300464,16),(512,512)])
        manual=manual_timing(result)
        self.assertTrue(math.isclose(result['duration_s'],manual['duration_s'],abs_tol=1e-12))
        for name,value in manual['components'].items():
            self.assertTrue(math.isclose(value,result['components'][name],rel_tol=1e-12))

    def test_bad_context_limits_fail_before_engine_creation(self):
        for value in (True,0,-1,300482,300481.,'300481'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate_compute(self.config(context_length_limit=value))

    def test_kahan_diagnostic_counts_retain_small_transfers_at_tb_scale(self):
        obj=object.__new__(GroupedStorage)
        obj.bytes_transferred=2.5e12;obj.disk_bytes_transferred=[2.5e12]
        obj._byte_correction=0.;obj._disk_byte_correction=[0.]
        amounts=[.0001]*10000
        for value in amounts:obj._account_transfer(0,value)
        expected=math.fsum([2.5e12,*amounts])
        self.assertEqual(obj.bytes_transferred,expected)
        self.assertEqual(obj.disk_bytes_transferred,[expected])

    def test_kahan_does_not_change_rates_or_read_completion(self):
        class Naive(GroupedStorage):
            def _account_transfer(self,disk,value):
                self.bytes_transferred+=value;self.disk_bytes_transferred[disk]+=value
        cfg=dict(disks=1,paths_per_disk=128,disk_Bps=180e9,path_Bps=2e9,
                 kv_bytes_per_token=109824,shared_link_Bps=180e9,
                 read_latency_s=0.,bandwidth_policy='max_min_fair')
        a,b=GroupedStorage(cfg,16),Naive(cfg,16)
        for i in range(30):
            for storage in (a,b):
                storage.submit_counted(0.,('r',i),{0:1000+i},16*109824/78,1e6+i,0.)
        while a.pending:
            self.assertEqual(a._group_rates(),b._group_rates())
            at=a.next_time();self.assertEqual(at,b.next_time())
            self.assertEqual(a.advance(at),b.advance(at))
            self.assertEqual(a.pending,b.pending)

    def test_formula_audit_rejects_false_context_provenance(self):
        timing=estimate_step(self.config(context_length_limit=300481),[(300464,16)])
        timing['formula_context_extrapolation']=False
        with self.assertRaises(AssertionError):manual_timing(timing)

    def test_startup_timeout_rejects_invalid_values_before_creating_output(self):
        from scripts.run_tfs_stack import run_one
        for value in (True,0,-1,float('nan'),float('inf'),601,'180'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                run_one(None,None,None,None,'immediate','first_token',admission_startup_timeout_s=value)
