"""Analytic independent-disk cases for offline bandwidth reconstruction."""
import importlib.util
from pathlib import Path
import unittest

P=Path(__file__).resolve().parents[2]/'scripts/report_tools/disk_bandwidth.py'
spec=importlib.util.spec_from_file_location('disk_replay_test',P)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class DiskReportTests(unittest.TestCase):
    def fixture(self,end=8.):
        cfg=dict(block_size=1,wall_time_scale=1,storage=dict(disks=2,disk_Bps=10.,shared_link_Bps=None,
            paths_per_disk=128,path_Bps=1.,kv_bytes_per_token=10.,read_latency_s=0.,bandwidth_policy='max_min_fair'))
        storage=dict(t0_monotonic_s=0.,hit_bindings={},layer_loads={'r':dict(counts={0:8,1:2},demand=1.)},
            layer_read_history=[dict(worker=0,rid='r',layer=0,submitted_s=0.,completed_s=end,
                disk_bytes=[80.,20.],bytes=100.,read_latency_s=0.)])
        return storage,cfg

    def test_hot_disk_cannot_borrow_idle_capacity(self):
        storage,cfg=self.fixture();band,check=module.reconstruct(storage,cfg,{'r'},0.,0.)
        self.assertEqual(check['status'],'passed');self.assertEqual(check['per_disk_bytes'],[80.,20.])
        self.assertEqual([(e['start_s'],e['end_s'],e['actual_Bps']) for e in band['intervals']],[(0.,2.,20.),(2.,8.,10.)])
        self.assertEqual(sum((e['end_s']-e['start_s'])*e['actual_Bps'] for e in band['intervals']),100.)

    def test_inconsistent_completion_is_rejected(self):
        storage,cfg=self.fixture(5.)
        with self.assertRaises(AssertionError):module.reconstruct(storage,cfg,{'r'},0.,0.)

    def test_disk_bins_conserve_integrals(self):
        storage,cfg=self.fixture();band,_=module.reconstruct(storage,cfg,{'r'},0.,0.)
        bins=module.disk_bins(band,8.,'test',width=.3)
        for disk,total in enumerate((80.,20.)):
            self.assertAlmostEqual(sum((r['end_s']-r['start_s'])*r['actual_Bps'] for r in bins if r['disk']==disk),total)
