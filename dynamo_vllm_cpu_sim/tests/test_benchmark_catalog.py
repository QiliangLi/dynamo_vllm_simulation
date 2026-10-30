"""Benchmark identity, corruption checks, discovery and export boundaries."""
import csv
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('portable_benchmark_catalog',REPO/'benchmark/manage.py')
manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)


class BenchmarkCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.name='demo_random_2_1s_2_1GBps';self.folder=self.root/self.name;self.folder.mkdir()
        cfg=dict(workers=2,wall_time_scale=1,block_size=16,max_model_len=33,
                 storage=dict(disks=1,disk_Bps=1e9,shared_link_Bps=1e9,kv_bytes_per_token=4),
                 admission=dict(reference_bandwidth_Bps=1e9),compute=dict(mode='linear'))
        for filename in ('config.json','config.historical.json'):
            (self.folder/filename).write_text(json.dumps(cfg))
        rows=[dict(id=f'r{i}',class_id='R',arrival_s=i,prompt_token_ids=[1]*32,
                   hit_tokens=16,u_tokens=16,remote_prefix_tokens=16,output_tokens=1) for i in range(2)]
        raw=''.join(json.dumps(r)+'\n' for r in rows).encode()
        (self.folder/'trace.jsonl.gz').write_bytes(gzip.compress(raw))
        with (self.folder/'requests.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['id','class_id','arrival_s','source_timestamp','input_tokens','hit_tokens','new_tokens','read_bytes'])
            writer.writeheader()
            for r in rows:writer.writerow(dict(id=r['id'],class_id='R',arrival_s=r['arrival_s'],source_timestamp=r['arrival_s'],input_tokens=32,hit_tokens=16,new_tokens=16,read_bytes=64))
        self.m=dict(case=self.name,source='demo',feature='random',requests=2,workers=2,arrival_window_s=1,
                    shared_storage_Bps=1e9,K=1,block_size=16,input_tokens=64,hit_tokens=32,new_tokens=32,read_bytes=128,
                    class_counts=dict(R=2),new_pct=50,aliases=['old-demo'],portable_runner_status='export_only',
                    runner_profile=None,allocation_scope='fixture',execution_feedback='disabled',description='fixture',
                    source_first_line=1,source_last_line=2,original_trace_sha256=hashlib.sha256(raw).hexdigest())
        self.save_manifest()
        self.patcher=patch.object(manager,'ROOT',self.root);self.patcher.start()

    def tearDown(self):
        self.patcher.stop();self.temp.cleanup()

    def save_manifest(self):
        self.m['files_sha256']={p.name:manager.sha(p) for p in self.folder.iterdir() if p.name!='manifest.json'}
        (self.folder/'manifest.json').write_text(json.dumps(self.m),encoding='utf-8')

    def test_new_folder_is_discovered_without_a_fixed_list(self):
        self.assertEqual(manager.case_names(),(self.name,))
        self.assertEqual(manager.case_names(supported_only=True),())
        self.assertEqual(manager.resolve_case('old-demo'),self.name)
        self.assertEqual(manager.verify()['requests_checked'],2)
        self.assertIn(self.name,manager.catalog_text())

    def test_filename_resource_mismatch_is_rejected_even_with_updated_hash(self):
        p=self.folder/'config.json';cfg=manager.read(p);cfg['workers']=3;p.write_text(json.dumps(cfg))
        self.save_manifest()
        with self.assertRaises(AssertionError):manager.verify()

    def test_input_corruption_is_rejected(self):
        with (self.folder/'trace.jsonl.gz').open('ab') as f:f.write(b'corrupt')
        with self.assertRaises(AssertionError):manager.verify()

    def test_export_preserves_input_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'export'
            result=manager.export(self.name,output,quiet=True)
            self.assertEqual(result['trace_sha256'],self.m['original_trace_sha256'])
            self.assertEqual(manager.read(output/'config.json'),manager.read(self.folder/'config.json'))
            with self.assertRaises(FileExistsError):manager.export(self.name,output,quiet=True)


class RunnerBoundaryTests(unittest.TestCase):
    def test_export_only_case_is_rejected_before_native_or_environment_access(self):
        r=subprocess.run([sys.executable,'-X','utf8',str(REPO/'dynamo_vllm_cpu_sim/scripts/run_benchmark.py'),
                          '--case','agentx_random_360_12s_12_180GBps','--dynamo-python','missing-overlay'],
                         capture_output=True,text=True,encoding='utf-8')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('仅支持校验',r.stderr)
        self.assertNotIn('Build the weighted',r.stderr)
