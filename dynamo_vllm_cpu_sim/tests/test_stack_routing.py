"""Reject a native non-minimum selection or leaked/incomplete load metrics."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from check_stack_routing import decisions,metrics_drained,positive_native_prefill_load
from run_tfs_stack import frontend_command
from run_sa_abc_stack import run


class StackRoutingTests(unittest.TestCase):
    def test_native_raw_load_can_evidence_activity_between_gauge_samples(self):
        self.assertTrue(positive_native_prefill_load('Formula (raw_prefill_blocks: 21.750, overlap_credit_blocks: 0.000)'))
        self.assertFalse(positive_native_prefill_load('Formula (raw_prefill_blocks: 0.000, overlap_credit_blocks: 1.000)'))
        self.assertFalse(positive_native_prefill_load('[ROUTING] Best: worker_1 cost=20'))

    def test_explicit_policy_and_legacy_default(self):
        self.assertEqual(frontend_command({},12)[-1],"round-robin")
        argv=frontend_command(dict(router_mode="kv"),12)
        self.assertEqual(argv[argv.index("--router-mode")+1],"kv")
        self.assertIn("--no-router-assume-kv-reuse",argv)
        with self.assertRaises(ValueError):frontend_command(dict(router_mode="other"),12)

    def test_unsupported_output_rejected_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"nested"/"experiment"
            with self.assertRaisesRegex(ValueError,"direct child"):
                run(SimpleNamespace(output=output))
            self.assertFalse(output.exists())

    def test_score_check_rejects_busy_target(self):
        text=('Formula for worker_id=11 dp_rank=0 with 0 effective cached blocks: 8.000 = x request_id="r"\n'
              'Formula for worker_id=12 dp_rank=0 with 0 effective cached blocks: 2.000 = x request_id="r"\n'
              '[ROUTING] Best: worker_12 dp_rank=0 request_id="r"\n')
        self.assertEqual(decisions(text,2)["unequal_load_decisions"],1)
        with self.assertRaises(AssertionError):decisions(text.replace("Best: worker_12","Best: worker_11"),2)
        with self.assertRaises(AssertionError):decisions(text,3)

    def test_load_cleanup_requires_both_gauges_and_finite_zero(self):
        lines=['dynamo_frontend_worker_active_prefill_tokens{worker_id="1"} 0',
               'dynamo_frontend_worker_active_decode_blocks{worker_id="1"} 0']
        self.assertTrue(metrics_drained(lines))
        self.assertFalse(metrics_drained([]))
        self.assertFalse(metrics_drained(lines[:1]))
        self.assertFalse(metrics_drained([lines[0],lines[1][:-1]+'1']))
        self.assertFalse(metrics_drained([lines[0],lines[1][:-1]+'NaN']))


if __name__=="__main__":unittest.main()
