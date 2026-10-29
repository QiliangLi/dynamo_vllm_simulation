"""A conserved total span must not hide scale-sensitive per-request latency."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.validate_time_scaling import analyze


class TimeScaleAnalysisTest(unittest.TestCase):
    def test_same_total_span_does_not_imply_same_request_latency(self):
        rows=[]
        for scale,latency,peak in ((50,2.,7),(100,4.,8)):
            for repeat in range(3):
                rows.append(dict(status="pass",phase="storage",mode="rpc",scale=scale,
                    metrics=dict(read_span_model_ms=4.,read_median_model_ms=latency,peak_readers=peak)))
        with tempfile.TemporaryDirectory() as temp:
            result=analyze(Path(temp),rows,.05)
        comparison=result["highest_scale_comparisons"][0]
        self.assertEqual(comparison["relative_change"]["read_span_model_ms"],0.)
        self.assertEqual(comparison["relative_change"]["read_median_model_ms"],.5)
        self.assertFalse(comparison["time_metrics_stable"])
        self.assertEqual(comparison["peaks"],[[7,7,7],[8,8,8]])

    def test_stack_requires_both_latency_and_compute_to_stabilize(self):
        rows=[dict(status="pass",phase="stack",mode="tfs",scale=scale,
                   metrics=dict(ttft_mean_ms=latency,compute_model_ms=compute,peak_readers=3))
              for scale,latency,compute in ((100,108.,280.),(200,108.,310.))]
        with tempfile.TemporaryDirectory() as temp:
            result=analyze(Path(temp),rows,.05)
        self.assertFalse(result["highest_scale_comparisons"][0]["time_metrics_stable"])


if __name__=="__main__":unittest.main()
