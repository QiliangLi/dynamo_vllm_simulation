"""Portable skill behavior and independent format-fixture invariants."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SKILL/'scripts'))
from plotting.dataset import validate_dataset
from plotting.report import metrics
from plotting.timeline import DataError
import redraw_timeline


class PortableReportTests(unittest.TestCase):
    def sample(self):
        return json.loads((SKILL/'examples/dataset.json').read_text(encoding='utf-8'))

    def test_sample_has_independently_known_time_and_byte_totals(self):
        result=metrics(validate_dataset(self.sample()))['summary'][0]
        self.assertEqual(result['requests'],4)
        self.assertEqual(result['completion_ms'],4000)
        self.assertAlmostEqual(result['mean_ttft_ms'],2175)
        self.assertAlmostEqual(result['slo_pct'],50)
        self.assertAlmostEqual(result['model_compute_s'],4.4)
        self.assertAlmostEqual(result['mean_bandwidth_GBps'],5)

    def test_wrong_read_integral_is_rejected(self):
        data=self.sample();data['cases'][0]['bandwidth']['intervals'][0]['actual_Bps']-=1e6
        with self.assertRaises(DataError):validate_dataset(data)

    def test_overlapping_worker_compute_is_rejected(self):
        data=self.sample();data['cases'][0]['comp'].append(dict(worker=0,start=1,end=1.8,state='light'))
        with self.assertRaises(DataError):validate_dataset(data)

    def test_missing_libraries_do_not_search_personal_runtimes(self):
        with patch.dict(os.environ,{},clear=True),patch.object(redraw_timeline.importlib.util,'find_spec',return_value=None),patch.object(redraw_timeline.subprocess,'run') as called:
            with self.assertRaises(RuntimeError):redraw_timeline.runtime_command()
            called.assert_not_called()

    def test_current_interpreter_is_not_recursively_relaunched(self):
        with patch.dict(os.environ,{'REPORT_ORGANIZER_PYTHON':sys.executable},clear=True),patch.object(redraw_timeline.importlib.util,'find_spec',return_value=None),patch.object(redraw_timeline.subprocess,'run') as called:
            with self.assertRaises(RuntimeError):redraw_timeline.runtime_command()
            called.assert_not_called()

    def test_cli_validation_works_with_only_the_supplied_dataset(self):
        env={k:v for k,v in os.environ.items() if not k.startswith('REPORT_ORGANIZER_')}
        proc=subprocess.run([sys.executable,'-X','utf8',str(SKILL/'scripts/generate_report.py'),
            '--dataset',str(SKILL/'examples/dataset.json'),'--validate-only'],env=env,
            capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(proc.returncode,0,proc.stderr)
        result=json.loads(proc.stdout)
        self.assertEqual(result['requests'],4)
        self.assertEqual(result['simulations_run'],0)

    def test_explicit_venv_can_share_the_system_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate=Path(directory)/'python'
            candidate.touch()
            with patch.dict(os.environ,{'REPORT_ORGANIZER_PYTHON':str(candidate)},clear=True),patch.object(redraw_timeline.importlib.util,'find_spec',return_value=None),patch.object(Path,'resolve',return_value=Path(sys.executable)),patch.object(redraw_timeline.subprocess,'run',return_value=subprocess.CompletedProcess([],0)):
                self.assertEqual(redraw_timeline.runtime_command(),str(candidate))


if __name__=='__main__':unittest.main()
