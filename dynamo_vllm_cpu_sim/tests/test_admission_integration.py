"""独立进程运行真实 Dynamo 选择器与固定 vLLM Scheduler。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gen_tfs_trace import PRESETS, write_trace


class AdmissionIntegrationTests(unittest.TestCase):
    def run_case(self, config, trace, directory):
        directory.mkdir(parents=True)
        path = directory / "config.json"
        path.write_text(json.dumps(config))
        result = subprocess.run([sys.executable, "-m", "sim.run", "--config", str(path),
                                 "--trace", str(trace), "--output", str(directory)],
                                cwd=ROOT, env={**os.environ, "OMP_NUM_THREADS": "1"},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        checked = subprocess.run([sys.executable, "scripts/check_results.py", str(directory)],
                                 cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        return json.loads((directory / "report.json").read_text())

    def test_all_modes_preserve_profiles_and_actual_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace.jsonl"
            write_trace(trace, 11, {**PRESETS["abc_burst"], "total": 12})
            c = json.loads((ROOT / "configs/tfs_demo.json").read_text())
            c["workers"] = 1
            reports = []
            for mode in ("immediate", "slack_only", "quota_fcfs", "tfs"):
                c["admission"]["mode"] = mode
                reports.append(self.run_case(c, trace, root / mode))
            profiles = [{rid: (s["pressure_Bps"], s["deadline_s"]) for rid, s in r["requests"].items()} for r in reports]
            self.assertTrue(all(p == profiles[0] for p in profiles))
            self.assertTrue(all(r["main_experiment_valid"] for r in reports))
            self.assertTrue(any(r["actual_batch"]["requests"]["p99"] > 1 for r in reports))
            self.assertGreater(reports[-1]["classes"]["A"]["central_wait_s"]["mean"], 0)

    def test_feedback_drain_does_not_extend_business_or_device_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace.jsonl"
            write_trace(trace, 11, {**PRESETS["abc_burst"], "total": 3,
                                   "remote_a": 0, "remote_b": 0})
            c = json.loads((ROOT / "configs/tfs_demo.json").read_text())
            c["workers"] = 1
            c["admission"].update(mode="immediate", release_on="load_done")
            plain = self.run_case(c, trace, root / "plain")
            c["admission"]["feedback_delay_s"] = .005
            delayed = self.run_case(c, trace, root / "delayed")
            self.assertEqual(plain["makespan_s"], delayed["makespan_s"])
            self.assertGreater(delayed["feedback_drained_s"], delayed["business_end_s"])
            self.assertEqual(plain["workers"][0]["compute_s"], delayed["workers"][0]["compute_s"])

    def test_arrival_just_after_compute_boundary_is_not_submitted_early(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = json.loads((ROOT / "configs/tfs_demo.json").read_text())
            c["workers"] = 1
            c["admission"]["mode"] = "immediate"
            boundary = c["compute"]["step_overhead_s"] + 16 * c["compute"]["prefill_token_s"]
            rows = [{"id": "r0", "arrival_s": 0.0, "prompt_token_ids": [1]*16, "output_tokens": 1},
                    {"id": "r1", "arrival_s": math.nextafter(boundary, math.inf),
                     "prompt_token_ids": [2]*16, "output_tokens": 1}]
            trace = root / "trace.jsonl"
            trace.write_text("\n".join(json.dumps(r) for r in rows))
            report = self.run_case(c, trace, root / "run")
            r = report["requests"]["r1"]
            self.assertGreaterEqual(r["submitted_s"], r["arrival_s"])
            self.assertGreaterEqual(r["central_wait_s"], 0)
