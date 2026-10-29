"""时间模型的语义边界；独立公式审计另见 scripts/check_prefill_formula.py。"""
import unittest
import hashlib
import json
from pathlib import Path

from sim.compute_time import estimate_step, reference_compute_s, validate_compute
from sim.timing_models.glm5_d950pr_reference import per_batch
from scripts.check_prefill_formula import manual, manual_legacy, manual_timing


class ComputeTimeTests(unittest.TestCase):
    def test_frozen_reference_fingerprint(self):
        folder = Path(__file__).resolve().parents[1]/"sim/timing_models"
        source = json.loads((folder/"source.json").read_text())
        self.assertEqual(hashlib.sha256((folder/"glm5_d950pr_reference.py").read_bytes()).hexdigest(),
                         source["sha256"])

    def test_admission_profile_uses_selected_model(self):
        from sim.request_profile import build_profile, normalize_config
        root = Path(__file__).resolve().parents[1]
        c = json.loads((root/"configs/demo.json").read_text())
        c["compute"] = {"mode":"glm5_d950pr"}
        c = normalize_config(c)
        row = dict(id="sample",arrival_s=0,prompt_token_ids=[1]*100,remote_prefix_tokens=16)
        got = build_profile(row,c)
        self.assertEqual(c["request_profile"]["mode"],"glm5_reference_v3")
        self.assertEqual((c['compute']['tp'], c['compute']['dp'], c['compute']['ep']), (1, 8, 8))
        self.assertAlmostEqual(got.standalone_compute_s,
                               estimate_step(c["compute"],[(16,84)])["duration_s"])
    def test_linear_batch_keeps_one_overhead(self):
        c = dict(step_overhead_s=50e-6, prefill_token_s=4e-6, decode_token_s=20e-6)
        self.assertAlmostEqual(estimate_step(c, [(0, 128)] * 4)["duration_s"], 0.002098)
        self.assertAlmostEqual(estimate_step(c, [(100, 3)], 2)["duration_s"], 102e-6)

    def test_independent_actual_batch_formula(self):
        for tp, dp in [(1, 8), (1, 12), (2, 4)]:
            for reuse in (True, False):
                c = dict(mode='glm5_d950pr', tp=tp, dp=dp, ep=tp*dp,
                         sparse_HBM_hit_ratio=.8, kv_hbm_reuse=reuse, engine_overhead_s=.001)
                for shapes in [[(0, 1)], [(8064, 128)]*4,
                               [(8064, 128), (4032, 64), (2016, 32), (1008, 16)]]:
                    got = estimate_step(c, shapes)
                    expected = manual(shapes, .8, .001, tp=tp, dp=dp, ep=tp*dp, reuse=reuse)
                    self.assertAlmostEqual(got['duration_s'], expected['duration_s'], places=12)
                    for name, value in expected['components'].items():
                        self.assertAlmostEqual(got['components'][name], value, places=8)
                    self.assertEqual(got['reference_dp'], dp)
                    self.assertEqual(got['batch_time_model'], 'glm5_actual_batch_work_v2')
                    self.assertAlmostEqual(got['per_layer_prefix_kv_gb'],sum(h for h,u in shapes)*1408/1e9)

    def test_legacy_audit_remains_source_equivalent(self):
        shapes = [(8064, 128), (4032, 64)]
        us, _ = per_batch([(h+u,u) for h,u in shapes])
        legacy = manual_legacy(shapes,.8)
        self.assertAlmostEqual(legacy['duration_s'],us*78e-6,places=12)
        timing = dict(shapes=shapes,sparse_HBM_hit_ratio=.8,engine_overhead_s=0)
        self.assertEqual(manual_timing(timing),legacy)

    def test_default_has_no_second_storage_fetch(self):
        got = estimate_step(dict(mode="glm5_d950pr"), [(1024, 256)])
        self.assertEqual(got["components"]["sparse_storage_us"], 0)
        self.assertEqual(got["engine_overhead_s"], 0)

    def test_chunk_context_and_actual_work_are_explicit(self):
        c = dict(mode="glm5_d950pr")
        early = estimate_step(c, [(0, 128)])
        late = estimate_step(c, [(4096, 128)])
        self.assertEqual(early["max_input_length"], 128)
        self.assertGreater(late["components"]["indexer_score_us"],
                           early["components"]["indexer_score_us"])
        batch = estimate_step(c, [(100, 10), (50, 5)])
        self.assertEqual(batch["real_prefill_tokens"], 15)
        self.assertEqual(batch["padded_prefill_tokens"], 15)
        self.assertEqual(batch["reference_dp"], 8)
        self.assertEqual(batch['work']['new_tokens'],15)
        self.assertAlmostEqual(sum(batch['layer_durations_us'])*1e-6,batch['model_s'])
        self.assertEqual(estimate_step(c,[(0,1)])['reference_dp'],batch['reference_dp'])

    def test_reference_chunks_use_same_clock(self):
        c = dict(mode="glm5_d950pr")
        expected = sum(estimate_step(c, [s])["duration_s"]
                       for s in [(100, 128), (228, 128), (356, 44)])
        self.assertAlmostEqual(reference_compute_s(c, 400, 100, 128), expected)

    def test_decode_and_oversize_fail_explicitly(self):
        c = dict(mode="glm5_d950pr")
        for shapes, decode in [([], 1), ([(0, 1)] * 9, 0), ([(0, 0)], 0)]:
            with self.subTest(shapes=shapes):
                with self.assertRaises(ValueError):
                    estimate_step(c, shapes, decode)
        with self.assertRaises(ValueError):
            validate_compute(dict(mode="glm5_d950pr", sparse_HBM_hit_ratio=1.1))
        for options in [dict(tp=0),dict(dp=True),dict(tp=3),dict(dp=8,ep=4),
                        dict(dp=257),dict(kv_hbm_reuse=1),dict(batch_time_model='old')]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_compute(dict(mode='glm5_d950pr',**options))

    def test_engine_overhead_is_explicit(self):
        base = dict(mode="glm5_d950pr")
        a = estimate_step(base, [(0, 128)])["duration_s"]
        b = estimate_step(dict(base, engine_overhead_s=0.001), [(0, 128)])["duration_s"]
        self.assertAlmostEqual(b - a, 0.001)


if __name__ == "__main__":
    unittest.main()
