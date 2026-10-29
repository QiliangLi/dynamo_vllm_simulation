"""Read concurrency is overlap of storage intervals, not HTTP in-flight count."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from audit_original_concurrency import overlap


class OriginalConcurrencyTest(unittest.TestCase):
    def test_tied_completion_and_submit_do_not_add_a_reader(self):
        result=overlap([dict(submitted_s=0,completed_s=1),dict(submitted_s=1,completed_s=2)])
        self.assertEqual(result["peak"],1)
        self.assertEqual(result["mean_concurrency"],1)
        self.assertEqual(result["multiple_fraction_of_span"],0)

    def test_real_overlap_and_idle_are_integrated(self):
        result=overlap([dict(submitted_s=0,completed_s=2),dict(submitted_s=1,completed_s=3),
                        dict(submitted_s=4,completed_s=5)])
        self.assertEqual(result["peak"],2)
        self.assertEqual(result["duration_by_concurrency_model_s"],{0:1,1:3,2:1})
        self.assertEqual(result["mean_concurrency"],1)
        self.assertEqual(result["multiple_fraction_of_span"],.2)


if __name__=="__main__":unittest.main()
