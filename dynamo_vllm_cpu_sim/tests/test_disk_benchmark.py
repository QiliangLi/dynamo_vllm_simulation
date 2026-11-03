"""Explicit independent-disk variants preserve input and legacy strategy defaults."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('disk_runner',ROOT/'scripts/run_benchmark.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class DiskBenchmarkTests(unittest.TestCase):
    def cfg(self):
        return json.loads((ROOT.parent/'benchmark/synthetic_abc_360_6s_12_90GBps/config.json').read_text(encoding='utf-8'))

    def test_independent_disks_remove_link_without_changing_reference_or_source(self):
        cfg=self.cfg();copy=json.loads(json.dumps(cfg))
        result=runner.apply_storage_layout(cfg,dict(disks=3,disk_Bps=30e9))
        self.assertEqual(cfg,copy);self.assertIsNone(result['storage']['shared_link_Bps'])
        self.assertEqual(result['storage']['disks']*result['storage']['disk_Bps'],90e9)
        self.assertEqual(result['admission'],cfg['admission'])
        for k in ('compute','request_profile','max_num_batched_tokens','max_num_seqs'):self.assertEqual(cfg[k],result[k])

    def test_bad_or_capacity_changing_layout_is_rejected(self):
        for layout in ({'disks':3,'disk_Bps':25e9},{'disks':True,'disk_Bps':90e9},
                       {'disks':3,'disk_Bps':float('nan')},{'disks':3,'disk_Bps':30e9,'shared_link_Bps':90e9}):
            with self.assertRaises(AssertionError):runner.apply_storage_layout(self.cfg(),layout)

    def test_five_arms_and_explicit_fcfs_vs_edf(self):
        cfg=self.cfg();m={'runner_profile':'synthetic_abc360_v1'}
        for arm in runner.ARMS:
            c=runner.arm_config(cfg,arm,m)
            self.assertEqual(c['request_ordering'],'fcfs' if arm=='direct-fcfs' else 'edf')
            self.assertEqual(c['routing_accounting']['enabled'],arm in ('tfs-a01','tfs-a1'))
        old=runner.arm_config(cfg,'old-tfs',m)
        self.assertEqual((old['admission']['mode'],old['admission']['placement'],old['admission']['classification']),('tfs','gateway','dynamic'))
        self.assertEqual(runner.DEFAULT_ARMS,('direct','tfs-a01','tfs-a1'))
        self.assertEqual(len(runner.ARMS),5)

    def test_frozen_special_arm_configs_remain_exact(self):
        for name in ('agentx_random_360_12s_12_180GBps','synthetic_abc_360_6s_12_90GBps'):
            folder=ROOT.parent/'benchmark'/name;m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            c=json.loads((folder/'run_contract.json').read_text(encoding='utf-8'))
            for arm,expected in c['arms'].items():self.assertEqual(runner.arm_config(c['arms']['direct'],arm,m),expected)


class NativeBackendContractTests(unittest.TestCase):
    def test_backend_selection_preserves_config_and_rejects_shared_link(self):
        cfg=json.loads((ROOT.parent/'benchmark/mooncake_abc_360_4s_12_100GBps/config.json').read_text(encoding='utf-8'))
        self.assertEqual(runner.apply_storage_backend(cfg),cfg)
        with self.assertRaises(ValueError):runner.apply_storage_backend(cfg,'cpp')
        disks=runner.apply_storage_layout(cfg,dict(disks=4,disk_Bps=25e9))
        cpp=runner.apply_storage_backend(disks,'cpp')
        self.assertEqual(cpp['storage']['backend'],'cpp')
        expected=json.loads(json.dumps(cpp));expected['storage'].pop('backend')
        self.assertEqual(expected,disks)
        with self.assertRaises(ValueError):runner.apply_storage_backend(disks,'other')
