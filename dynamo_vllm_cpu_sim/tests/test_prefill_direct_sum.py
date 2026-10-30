"""直接组合模式：独立算术、扣减边界、逐层执行和画像兼容。"""
import copy
import json
import math
from pathlib import Path
import unittest

from sim.compute_time import estimate_step, validate_compute
from sim.timing_models.prefill_time_batch import (
    DIRECT_SUM_VERSION, VERSION, estimate_batch, per_batch)
from scripts.check_prefill_formula import manual_direct_sum, manual_timing


class DirectSumTests(unittest.TestCase):
    def batch(self, rows, fraction=1.0, **options):
        return estimate_batch(rows, dp=8, ep=8, batch_time_model=DIRECT_SUM_VERSION,
                              launch_saving_fraction=fraction, **options)

    def test_five_requests_save_only_three_duplicate_launches_per_layer(self):
        rows = [(4096, 4096)] + [(512, 512)] * 4
        singles = [estimate_batch([row], dp=8, ep=8) for row in rows]
        batch = self.batch(rows)
        self.assertAlmostEqual(math.fsum(s['total_us'] for s in singles) - batch['total_us'],
                               4 * 3 * 7 * 78, places=7)
        self.assertEqual(batch['batch_composition']['saved_launch_us_total'], 6552)
        for name, actual in batch['components_us'].items():
            expected = math.fsum(s['components_us'][name] for s in singles)
            if name in ('indexer_score_us', 'gather_us', 'sparse_attention_us'):
                expected -= 4 * 7
            self.assertAlmostEqual(actual, expected, places=8, msg=name)
        self.assertEqual(batch['work']['new_tokens'], 6144)
        self.assertEqual(batch['deployment'], dict(tp=1, dp=8, ep=8))

    def test_zero_fraction_is_exact_singleton_sum(self):
        rows = [(13000, 17), (512, 512), (2, 1)]
        batch = self.batch(rows, 0)
        total = math.fsum(estimate_batch([r], dp=8, ep=8)['total_us'] for r in rows)
        self.assertAlmostEqual(batch['total_us'], total, places=7)
        self.assertEqual(batch['batch_composition']['saved_launch_us_total'], 0)

    def test_fraction_and_independent_arithmetic_for_fixed_deployments(self):
        shapes = [(8192, 128), (4032, 64), (0, 512), (1, 1)]
        for tp, dp in ((1, 8), (1, 12), (2, 4)):
            for reuse in (True, False):
                for fraction in (0, .25, 1):
                    c = dict(mode='glm5_d950pr', batch_time_model=DIRECT_SUM_VERSION,
                        tp=tp, dp=dp, ep=tp*dp, kv_hbm_reuse=reuse,
                        sparse_HBM_hit_ratio=.8, engine_overhead_s=.001,
                        launch_saving_fraction=fraction)
                    with self.subTest(tp=tp, dp=dp, reuse=reuse, fraction=fraction):
                        got = estimate_step(c, shapes)
                        expected = manual_direct_sum(shapes, .8, .001, tp=tp, dp=dp,
                                                      ep=tp*dp, reuse=reuse, fraction=fraction)
                        self.assertAlmostEqual(got['duration_s'], expected['duration_s'], places=12)
                        self.assertEqual(manual_timing(got), expected)
                        for name, value in expected['components'].items():
                            self.assertAlmostEqual(got['components'][name], value, places=8)
                        self.assertAlmostEqual(math.fsum(got['layer_durations_us'])*1e-6+.001,
                                               got['duration_s'], places=12)

    def test_single_request_time_does_not_change(self):
        for rows in ([(1, 1)], [(4096, 4096)], [(131072, 1)]):
            one = estimate_batch(rows, dp=8, ep=8)
            for fraction in (0, .5, 1):
                got = self.batch(rows, fraction)
                for field in ('total_us', 'components_us', 'work', 'layer_durations_us',
                              'prefix_kv_bytes'):
                    self.assertEqual(got[field], one[field], field)

    def test_zero_work_does_not_save_a_launch_but_keeps_prefix_bytes(self):
        for rows in ([], [(512, 0)], [(512, 0), (256, 0)]):
            got = self.batch(rows)
            self.assertEqual(got['total_us'], 0)
            self.assertEqual(got['batch_composition']['saved_launch_us_total'], 0)
            self.assertEqual(got['prefix_kv_bytes'], sum(h for h,u in rows)*109824)
        a = self.batch([(100, 50)])
        b = self.batch([(100, 50), (512, 0)])
        self.assertEqual(a['total_us'], b['total_us'])
        self.assertEqual(b['batch_size'], 1)

    def test_smallest_eight_request_batch_stays_positive(self):
        batch = self.batch([(1, 1)] * 8)
        self.assertTrue(all(t > 0 for t in batch['layer_durations_us']))
        self.assertTrue(all(t >= 0 for t in batch['components_us'].values()))

    def test_no_weight_or_communication_startup_amortization(self):
        one = estimate_batch([(512, 512)], dp=8, ep=8)
        merged = self.batch([(512, 512)] * 4)
        for key in ('moe_us', 'dispatch_us', 'combine_us', 'mla_projection_us'):
            self.assertEqual(merged['components_us'][key], one['components_us'][key] * 4)
        self.assertEqual(len(merged['moe_work']['singletons']), 4)
        self.assertEqual(merged['moe_work']['group_routed_token_expert_work'],
                         one['moe_work']['group_routed_token_expert_work'] * 4)

    def test_invalid_options_fail_before_execution(self):
        for value in (-1, 1.1, float('nan'), float('inf'), True, '0.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.batch([(512, 512)], value)
            with self.assertRaises(ValueError):
                validate_compute(dict(mode='glm5_d950pr', batch_time_model=DIRECT_SUM_VERSION,
                                      launch_saving_fraction=value))
        with self.assertRaises(ValueError):
            estimate_step(dict(mode='glm5_d950pr', launch_saving_fraction=.5), [(0, 1)])
        with self.assertRaises(ValueError):
            self.batch([(1, 1)] * 9)
        with self.assertRaises(ValueError):
            estimate_step(dict(mode='glm5_d950pr', batch_time_model=DIRECT_SUM_VERSION), [], 1)

    def test_default_mode_and_per_batch_units(self):
        rows = [(4096, 4096)] + [(512, 512)] * 3
        self.assertEqual(estimate_batch(rows, dp=8, ep=8)['model'], VERSION)
        self.assertAlmostEqual(estimate_batch(rows, dp=8, ep=8)['total_ms'], 901.18525248)
        got = self.batch(rows)
        us, gb = per_batch(rows, dp=8, ep=8, batch_time_model=DIRECT_SUM_VERSION)
        self.assertAlmostEqual(us * 78, got['total_us'])
        self.assertEqual(gb, got['per_layer_prefix_kv_gb'])

    def test_normalized_config_and_chunked_reference_profile_are_unchanged(self):
        from sim.request_profile import normalize_config, build_profile
        from sim.reference_time import get_reference_time_estimator
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root/'configs/mooncake_first_seen_layerwise.json').read_text())
        config['request_profile']['profile_token_budget'] = 128
        old = normalize_config(config)
        config['compute']['batch_time_model'] = DIRECT_SUM_VERSION
        new = normalize_config(config)
        self.assertEqual(new['compute']['launch_saving_fraction'], 1)
        row = dict(id='sample', arrival_s=0, prompt_token_ids=[1]*1024, remote_prefix_tokens=512)
        self.assertEqual(build_profile(row, old), build_profile(row, new))
        for protocol in ('bulk', 'layer_pipeline', 'layer_serial'):
            old['kv_transfer_mode'] = new['kv_transfer_mode'] = protocol
            a = get_reference_time_estimator(old).estimate(1024, 512)
            b = get_reference_time_estimator(new).estimate(1024, 512)
            self.assertEqual(a, b)

    def test_no_mutation_and_request_order_invariance(self):
        rows = [[8192, 128], [512, 512], [400, 32]]
        before = copy.deepcopy(rows)
        a, b = self.batch(rows), self.batch(iter(reversed(rows)))
        self.assertEqual(rows, before)
        self.assertEqual(a['components_us'], b['components_us'])
        self.assertEqual(a['total_us'], b['total_us'])


if __name__ == '__main__':
    unittest.main()
