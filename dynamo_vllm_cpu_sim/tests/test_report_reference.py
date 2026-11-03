"""The report generator can validate its own current CSV schema and legacy CSVs."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

P=Path(__file__).resolve().parents[2]/'scripts/report_tools'
sys.path.insert(0,str(P))
from plotting.report import check_reference
from plotting.timeline import DataError


class ReportReferenceTests(unittest.TestCase):
    def fixture(self,folder,legacy=False):
        data=folder/'report_data';data.mkdir()
        (data/'run_summary.csv').write_text('mode,completion_ms\ndirect,1000\n')
        header='mode,worker,model_compute_s,io_without_model_compute_s,other_s' if legacy else 'case,worker,model_s,red_s,gray_s'
        (data/'device_utilization.csv').write_text(header+'\ndirect,0,0.5,0.2,0.3\n')
        return dict(summary=[dict(mode='direct',completion_ms=1000)],
                    workers=[dict(case='direct',worker=0,model_s=.5,red_s=.2,gray_s=.3)])

    def test_current_and_legacy_account_columns_are_compared(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);result=self.fixture(p,legacy)
                self.assertEqual(check_reference(result,p)['numeric_checks'],4)

    def test_changed_current_account_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);result=self.fixture(p);result['workers'][0]['gray_s']=.4
            with self.assertRaises(DataError):check_reference(result,p)
