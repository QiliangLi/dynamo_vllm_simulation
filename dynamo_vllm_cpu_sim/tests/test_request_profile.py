"""画像固定、单位和公开信息边界的行为测试。"""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from sim.request_profile import build_profile, normalize_config

ROOT = Path(__file__).resolve().parents[1]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.c = normalize_config(json.loads((ROOT / "configs/demo.json").read_text()))
        self.row = {"id": "r", "arrival_s": 1, "prompt_token_ids": [1] * 32,
                    "remote_prefix_tokens": 32, "output_tokens": 1, "class_id": "A"}

    def test_whole_prompt_cannot_be_restored(self):
        p = build_profile(self.row, self.c)
        self.assertEqual(p.estimated_remote_tokens, 16)
        self.assertEqual(p.estimated_read_bytes, 16 * 131072)
        self.assertAlmostEqual(p.pressure_Bps * p.standalone_compute_s, p.estimated_read_bytes)
        self.assertAlmostEqual(p.deadline_s, 1 + 4 * p.reference_service_s)

    def test_capacity_does_not_change_profile(self):
        changed = deepcopy(self.c)
        changed.update(max_num_seqs=1, max_num_batched_tokens=16)
        self.assertEqual(build_profile(self.row, self.c), build_profile(self.row, changed))

    def test_storage_demand_and_class_are_not_pressure(self):
        p = build_profile(self.row, self.c)
        self.row.update(demand_Bps=1, class_id="other", local_cache_hit_tokens=10000)
        self.assertEqual(p.pressure_Bps, build_profile(self.row, self.c).pressure_Bps)

    def test_zero_read(self):
        self.row["remote_prefix_tokens"] = 0
        p = build_profile(self.row, self.c)
        self.assertEqual(p.pressure_Bps, 0)
        self.assertEqual(p.reference_service_s, p.standalone_compute_s)

    def test_invalid_parameters_fail_before_engine(self):
        for group, key, value in [("admission", "quantile", 2), ("admission", "feedback_delay_s", -1),
                                  ("admission", "aging_cooldown_s", 0), ("admission", "release_on", "bad"),
                                  ("admission", "reference_bandwidth_Bps", float("nan")),
                                  ("request_profile", "profile_token_budget", 0),
                                  ("request_profile", "slo_multiplier", -1)]:
            with self.subTest(key=key):
                c = deepcopy(self.c)
                c[group][key] = value
                with self.assertRaises(ValueError):
                    normalize_config(c)
