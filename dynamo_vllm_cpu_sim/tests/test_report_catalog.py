"""Archive verification detects changed source tables and unsafe paths."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('report_archive_catalog',REPO/'report/manage.py')
manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)


class ReportCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.repo=Path(self.temp.name)
        self.root=self.repo/'report';self.benchmark='demo_random_1_1s_1_1GBps'
        self.folder=self.root/self.benchmark/'20261030-120000_direct-EDF';self.folder.mkdir(parents=True)
        benchmark=self.repo/'benchmark'/self.benchmark;benchmark.mkdir(parents=True)
        (benchmark/'manifest.json').write_text(json.dumps(dict(original_trace_sha256='input-sha')))
        data=self.folder/'report_data';data.mkdir()
        table=b'mode,mean_ttft_ms\ndirect,10\n';(data/'run_summary.csv').write_bytes(table)
        (data/'source_validation.json').write_text(json.dumps(dict(status='passed',visual_reviewed=True)))
        image=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6ioAAAAASUVORK5CYII=')
        (self.folder/'figure').mkdir();(self.folder/'figure/test.png').write_bytes(image)
        (self.folder/'report.md').write_text('# Result\n\n![image](figure/test.png)\n[check](archive_validation.json)\n',encoding='utf-8')
        self.m=dict(report_id=self.folder.relative_to(self.root).as_posix(),benchmark_id=self.benchmark,
                    study_id='same-study',created_at='2026-10-30T12:00:00+08:00',experiment_dates=['2026-10-29'],
                    benchmark_input_sha256='input-sha',simulations_run=0,performance_recomputed=False,
                    compatibility='matched_input_and_model',schemes=[dict(label='direct EDF')],
                    standard_dataset=None,source_visual_reviewed=True,
                    source_files={'report_data/run_summary.csv':dict(sha256=hashlib.sha256(table).hexdigest())},
                    figures=[dict(path='figure/test.png',source_sha256=hashlib.sha256(image).hexdigest(),width=1,height=1)],files={})
        self.save()
        self.patches=[patch.object(manager,'ROOT',self.root),patch.object(manager,'REPO',self.repo)]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def save(self):
        self.m['files']={p.relative_to(self.folder).as_posix():dict(sha256=manager.digest(p),bytes=p.stat().st_size)
                         for p in self.folder.rglob('*') if p.is_file() and p.name not in ('manifest.json','archive_validation.json')}
        (self.folder/'manifest.json').write_text(json.dumps(self.m),encoding='utf-8')

    def test_first_archive_validation_can_be_created_and_checked(self):
        result=manager.verify(write=True)
        self.assertEqual(result['report_entries'],1)
        self.assertEqual(manager.verify()['status'],'passed')
        self.assertIn(self.benchmark,manager.catalog_text())

    def test_source_table_change_is_rejected_even_with_new_archive_hash(self):
        (self.folder/'report_data/run_summary.csv').write_bytes(b'mode,mean_ttft_ms\ndirect,1\n')
        self.save()
        with self.assertRaises(AssertionError):manager.verify(write=True)

    def test_manifest_cannot_escape_its_report_folder(self):
        outside=self.root/'outside.txt';outside.write_text('outside')
        self.m['files']['../../outside.txt']=dict(sha256=manager.digest(outside),bytes=outside.stat().st_size)
        (self.folder/'manifest.json').write_text(json.dumps(self.m))
        with self.assertRaises(AssertionError):manager.verify(write=True)

    def test_missing_figure_is_rejected(self):
        (self.folder/'figure/test.png').unlink()
        with self.assertRaises(AssertionError):manager.verify(write=True)

    def test_legacy_report_does_not_claim_generic_redraw(self):
        manager.verify(write=True)
        with patch.object(manager.subprocess,'run') as run:
            with self.assertRaises(SystemExit):manager.redraw(self.m['report_id'],self.repo/'output')
            run.assert_not_called()
