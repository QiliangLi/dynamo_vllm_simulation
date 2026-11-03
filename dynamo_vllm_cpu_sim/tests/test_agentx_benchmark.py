"""AgentX input identity, explicit runtime contract and smoke selection."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class AgentXBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner=load('agentx_benchmark_runner',ROOT/'scripts/run_benchmark.py')
        cls.folder=ROOT.parent/'benchmark/agentx_random_360_12s_12_180GBps'
        cls.manifest=json.loads((cls.folder/'manifest.json').read_text(encoding='utf-8'))
        cls.contract=json.loads((cls.folder/'run_contract.json').read_text(encoding='utf-8'))

    def test_supported_catalog_and_frozen_input_identity(self):
        result=self.runner.MANAGER.verify_case(self.folder.name)
        self.assertEqual(result['portable_runner_status'],'supported')
        self.assertEqual(result['requests'],360)
        self.assertEqual(self.manifest['original_trace_sha256'],
            'eeafecc40e2eb2e5185875e872716056531daecb13bcb768fa9555efacf2e707')
        self.assertIn(self.folder.name,self.runner.CASES)

    def test_guard_is_injected_without_mutating_frozen_arm_configs(self):
        before=copy.deepcopy(self.contract)
        for arm,cfg in self.contract['arms'].items():
            effective=self.runner.effective_config(cfg,self.manifest,self.contract)
            self.assertEqual(effective['compute'].pop('context_length_limit'),300481)
            self.assertEqual(effective,cfg)
        self.assertEqual(self.contract,before)
        with self.assertRaises(AssertionError):self.runner.effective_config(before['arms']['direct'],self.manifest)

    def test_unrelated_profile_has_no_implicit_length_extension(self):
        cfg=self.contract['arms']['direct']
        self.assertEqual(self.runner.effective_config(cfg,{'runner_profile':'mooncake360_v1'}),cfg)

    @unittest.skipIf(importlib.util.find_spec('psutil') is None,
                     'psutil runtime dependency is absent; selection is covered by the Linux suite')
    def test_smoke_contains_longest_and_median_without_changing_original_rows(self):
        runtime=load('agentx_benchmark_runtime_test',ROOT/'scripts/benchmark_runtime.py')
        rows=[dict(id=f'agentx:{i}',class_id='A' if i<3 else 'B',arrival_s=i,
                   prompt_token_ids=[1]*n) for i,n in enumerate((16,128,300480,32,131073,200000))]
        before=copy.deepcopy(rows)
        selected=runtime.smoke_requests(rows,long_context=True)
        self.assertEqual(len(selected),6)
        self.assertIn('agentx:2',[r['id'] for r in selected])
        self.assertTrue(all(r['arrival_s']==0 for r in selected))
        self.assertEqual(rows,before)
        self.assertEqual(len(runtime.smoke_requests(rows)),4)
