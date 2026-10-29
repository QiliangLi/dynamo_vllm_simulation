"""负载与矩阵控制变量测试，不启动设备仿真。"""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.gen_tfs_trace import generate, PRESETS
from scripts.run_tfs_matrix import expand

ROOT = Path(__file__).resolve().parents[1]


class TfsToolTests(unittest.TestCase):
    def test_trace_is_reproducible_and_has_unique_prefixes(self):
        for preset in PRESETS.values():
            rows = generate(11, preset)
            self.assertEqual(rows, generate(11, preset))
            self.assertEqual(len({tuple(r["prompt_token_ids"][:16]) for r in rows}), len(rows))
            self.assertEqual({r["class_id"] for r in rows}, {"A", "B", "C"})
            self.assertTrue(all(r["output_tokens"] < 16 for r in rows))

    def test_matrix_shares_trace_and_rejects_axis_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = json.loads((ROOT / "configs/tfs_demo.json").read_text())
            (root / "base.json").write_text(json.dumps(c))
            spec = {"schema_version": 1, "base_config": "base.json", "seeds": [11],
                    "workloads": [{"name": "burst", "generator_preset": "abc_burst"}],
                    "axes": {"admission.mode": ["immediate", "tfs"], "max_num_seqs": [1, 4]},
                    "fixed": {"policy": "fcfs"}}
            path = root / "spec.json"
            path.write_text(json.dumps(spec))
            jobs = expand(path, root / "output")
            self.assertEqual(len(jobs), 4)
            self.assertEqual(len({j["trace_file_sha256"] for j in jobs}), 1)
            self.assertEqual(len({j["trace"] for j in jobs}), 1)
            spec["fixed"]["max_num_seqs"] = 8
            path.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, "both fixed and axes"):
                expand(path, root / "invalid")
