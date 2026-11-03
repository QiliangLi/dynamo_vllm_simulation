"""Frozen synthetic ABC configuration, planned arrivals and contract rejection."""
import copy
import csv
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class SyntheticBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner=load('synthetic_benchmark_runner',ROOT/'scripts/run_benchmark.py')
        cls.folder=ROOT.parent/'benchmark/synthetic_abc_360_6s_12_90GBps'
        read=lambda p:json.loads(p.read_text(encoding='utf-8'))
        cls.manifest=read(cls.folder/'manifest.json');cls.contract=read(cls.folder/'run_contract.json')
        cls.cfg=read(cls.folder/'config.json')
        from sim.compute_time import estimate_step
        times={name:estimate_step(cls.cfg['compute'],[(s['hit_tokens'],s['new_tokens'])])['duration_s']
               for name,s in cls.manifest['class_shapes'].items()}
        with (cls.folder/'requests.csv').open(encoding='utf-8',newline='') as f:
            cls.profiles={r['id']:dict(id=r['id'],class_id=r['class_id'],n=int(r['input_tokens']),
                h=int(r['hit_tokens']),u=int(r['new_tokens']),arrival_s=float(r['arrival_s']),
                compute_s=times[r['class_id']]) for r in csv.DictReader(f)}

    def test_catalog_support_and_input_identity(self):
        check=self.runner.MANAGER.verify_case(self.folder.name)
        self.assertEqual(check['portable_runner_status'],'supported')
        self.assertEqual(check['requests'],360)
        self.assertIn(self.folder.name,self.runner.CASES)
        self.assertEqual(self.manifest['original_trace_sha256'],
            '3148c9e7cf8952f9c016ba9ac7e1db8993780d96bb2146f99d7abcfb01ec40b6')

    def test_no_guard_or_config_override_and_historical_configs_preserved(self):
        for cfg in self.contract['arms'].values():
            self.assertEqual(self.runner.effective_config(cfg,self.manifest,self.contract),cfg)
            self.assertNotIn('context_length_limit',cfg['compute'])
        historical=json.loads((self.folder/'config.historical.json').read_text(encoding='utf-8'))
        self.assertEqual(self.cfg,historical)

    def test_contract_rejects_other_loads_quota_or_timing(self):
        verify=self.runner.MANAGER.verify_synthetic_contract
        verify(self.manifest,self.cfg,self.contract)
        for key,value in [('arrival_window_s',4),('shared_storage_Bps',180e9)]:
            m=copy.deepcopy(self.manifest);m[key]=value
            with self.assertRaises(AssertionError):verify(m,self.cfg,self.contract)
        for key,value in [('expected_stable_heavy_cap',2),('execution_feedback','sync_batch')]:
            c=copy.deepcopy(self.contract);c[key]=value
            with self.assertRaises(AssertionError):verify(self.manifest,self.cfg,c)
        c=copy.deepcopy(self.contract);c['arms']['tfs-a1']['routing_accounting']['waiting_factor']=.1
        with self.assertRaises(AssertionError):verify(self.manifest,self.cfg,c)

    def test_plan_keeps_nonzero_first_arrival_and_fixed_shapes(self):
        self.runner.audit_synthetic_plan(self.profiles,self.manifest,self.contract)
        bad=copy.deepcopy(self.profiles);next(iter(bad.values()))['arrival_s']=0.
        with self.assertRaises(AssertionError):self.runner.audit_synthetic_plan(bad,self.manifest,self.contract)
        bad=copy.deepcopy(self.profiles);next(iter(bad.values()))['u']+=1
        with self.assertRaises(AssertionError):self.runner.audit_synthetic_plan(bad,self.manifest,self.contract)

    def test_plan_rejects_changed_reference_compute(self):
        bad=copy.deepcopy(self.profiles);next(iter(bad.values()))['compute_s']+=.1
        with self.assertRaises(AssertionError):self.runner.audit_synthetic_plan(bad,self.manifest,self.contract)
