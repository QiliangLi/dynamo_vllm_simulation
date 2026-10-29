"""检查正式组批函数的公式兼容性、请求边界、固定部署与无全局修改。"""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
from pathlib import Path
import random
import unittest

from sim.timing_models.prefill_time_batch import REFERENCE, REFERENCE_SHA256, _reference, estimate_batch, per_batch


class Glm5BatchTests(unittest.TestCase):
    def test_unchanged_operators_against_frozen_formulas(self):
        """保留的非 MoE 算子仍与原公式一致；不把旧 MoE 结果当正确性标准。"""
        ref = _reference()
        rng = random.Random(20261028)
        for _ in range(100):
            bs = rng.randint(1, 8)
            tp = rng.choice((1, 2, 4))
            dp = rng.choice((1, 4, 8, 12, 32))
            history, new = rng.randint(0, 30000), rng.randint(1, 8192)
            hit, reuse = rng.choice((0.0, 0.8, 1.0)), rng.choice((True, False))
            requests = [(history + new, new)] * bs
            with self.subTest(bs=bs, tp=tp, dp=dp, h=history, u=new, hit=hit, reuse=reuse):
                cfg = dict(ref.CONF, bs=bs, tp=tp, dp=dp, ep=tp * dp, total_bs=bs * dp,
                           full_seq_len=history + new, new_query_len=new,
                           sparse_HBM_hit_ratio=hit, kv_hbm_reuse=reuse)
                dense = ref._t_dense_layer(ref.MODEL, cfg, ref.NPU)
                got = estimate_batch(requests, tp=tp, dp=dp, ep=tp * dp,
                                     sparse_HBM_hit_ratio=hit, kv_hbm_reuse=reuse)
                self.assertTrue(math.isclose(got["dense_layer_us"], dense, rel_tol=1e-12))
                for key, function in (("indexer_q_us", ref._t_indexer_qb_linear),
                                      ("indexer_score_us", ref._t_indexer_qk_score),
                                      ("gather_us", ref._t_gather_kv),
                                      ("sparse_attention_us", ref._t_fused_mla_attention),
                                      ("mla_projection_us", ref._t_mla_proj),
                                      ("sparse_storage_us", ref._t_sparse_kv_load),
                                      ("dense_mlp_us", ref._t_mlp)):
                    expected = function(ref.MODEL, cfg, ref.NPU)
                    self.assertTrue(math.isclose(got["components_us"][key], expected,
                                                 rel_tol=1e-12, abs_tol=1e-12), key)

    def test_moe_preserves_reference_when_original_work_is_complete(self):
        ref = _reference()
        for bs, dp, tp in ((4, 8, 1), (1, 32, 1), (8, 4, 1), (2, 16, 2)):
            cfg = dict(ref.CONF, bs=bs, dp=dp, ep=dp * tp, tp=tp,
                       total_bs=bs * dp, new_query_len=512, full_seq_len=512,
                       sparse_HBM_hit_ratio=1.0)
            result = estimate_batch([(512, 512)] * bs, dp=dp, ep=dp * tp, tp=tp)
            for key, function in (("moe_us", ref._t_moe),
                                  ("dispatch_us", ref._t_moe_dispatch),
                                  ("combine_us", ref._t_moe_combine)):
                self.assertTrue(math.isclose(result["components_us"][key],
                                             function(ref.MODEL, cfg, ref.NPU), rel_tol=1e-12))

    def test_512_example_conserves_work_and_bytes(self):
        one = estimate_batch([(512, 512)], dp=8, ep=8)["moe_work"]
        batch = estimate_batch([(512, 512)] * 4, dp=8, ep=8)["moe_work"]
        self.assertEqual(one["group_new_tokens"], 4096)
        self.assertEqual(one["group_routed_token_expert_work"], 32768)
        self.assertEqual(one["ranks"][0]["routed_token_expert_work"], 4096)
        self.assertEqual(batch["ranks"][0]["routed_token_expert_work"], 16384)
        m = _reference().MODEL
        self.assertEqual(one["ranks"][0]["routed_flops"], 6 * 4096 * m["dim"] * m["moe_inter_dim"])
        for first, merged in zip(one["ranks"], batch["ranks"]):
            for name in ("routed_token_expert_work", "shared_token_expert_work",
                         "routed_flops", "shared_flops", "routed_activation_bytes",
                         "shared_activation_bytes", "dispatch_logical_bytes", "combine_logical_bytes"):
                self.assertEqual(merged[name], first[name] * 4, name)
            self.assertEqual(merged["routed_weight_bytes"], first["routed_weight_bytes"])
            self.assertEqual(merged["shared_weight_bytes"], first["shared_weight_bytes"])

    def test_random_regroupings_conserve_work_on_every_rank(self):
        rng = random.Random(416)
        for _ in range(100):
            dp, tp = rng.choice((1, 3, 8, 12, 32)), rng.choice((1, 2, 4))
            lengths = [rng.randint(1, 4000) for _ in range(rng.randint(2, 8))]
            merged = estimate_batch([(n, n) for n in lengths], dp=dp, ep=dp * tp, tp=tp)["moe_work"]
            singles = [estimate_batch([(n, n)], dp=dp, ep=dp * tp, tp=tp)["moe_work"] for n in lengths]
            self.assertEqual(sum(r["owned_experts"] for r in merged["ranks"]), 256)
            self.assertTrue(math.isclose(sum(r["routed_token_expert_work"] for r in merged["ranks"]),
                                        sum(lengths) * dp * 8, rel_tol=1e-12))
            self.assertTrue(math.isclose(sum(r["shared_token_expert_work"] for r in merged["ranks"]),
                                        sum(lengths) * dp, rel_tol=1e-12))
            for rank, row in enumerate(merged["ranks"]):
                for name in ("routed_token_expert_work", "shared_token_expert_work",
                             "routed_flops", "shared_flops", "routed_activation_bytes",
                             "shared_activation_bytes", "dispatch_logical_bytes", "combine_logical_bytes"):
                    expected = math.fsum(single["ranks"][rank][name] for single in singles)
                    self.assertTrue(math.isclose(row[name], expected, rel_tol=1e-12), name)

    def test_small_batch_does_not_create_minimum_one_token_per_expert(self):
        for dp in (1, 8):
            work = estimate_batch([(1, 1)], dp=dp, ep=dp)["moe_work"]
            self.assertEqual(work["group_routed_token_expert_work"], dp * 8)
            self.assertEqual(work["expected_tokens_per_expert"], dp * 8 / 256)
            self.assertLessEqual(work["group_expected_active_experts"], dp * 8)
            self.assertLess(work["group_expected_active_experts"], 256)
            self.assertAlmostEqual(sum(r["routed_token_expert_work"] for r in work["ranks"]), dp * 8)
        one = estimate_batch([(1, 1)], dp=1, ep=1)["moe_work"]
        self.assertAlmostEqual(one["group_expected_active_experts"], 8)
        self.assertAlmostEqual(one["expected_tokens_per_active_expert"], 1)

    def test_uneven_placement_has_exact_expert_count_and_critical_rank(self):
        result = estimate_batch([(512, 512)], dp=12, ep=12)
        work = result["moe_work"]
        self.assertEqual([r["owned_experts"] for r in work["ranks"]], [22] * 4 + [21] * 8)
        self.assertEqual(work["critical_rank"], 0)
        self.assertAlmostEqual(sum(r["routed_token_expert_work"] for r in work["ranks"]), 512 * 12 * 8)
        self.assertGreater(work["ranks"][0]["moe_us"], work["ranks"][-1]["moe_us"])
        self.assertEqual(result["components_us"]["moe_us"], work["ranks"][0]["moe_us"])

    def test_active_expert_expectation_only_changes_weight_read_count(self):
        one = estimate_batch([(1, 1)], dp=1, ep=1)["moe_work"]["ranks"][0]
        merged = estimate_batch([(1, 1)] * 4, dp=1, ep=1)["moe_work"]["ranks"][0]
        self.assertEqual(merged["routed_flops"], one["routed_flops"] * 4)
        self.assertLess(merged["routed_weight_bytes"], one["routed_weight_bytes"] * 4)
        self.assertEqual(merged["shared_weight_bytes"], one["shared_weight_bytes"])

    def test_communication_preserves_payload_but_amortizes_startup(self):
        singles = [estimate_batch([(n, n)], dp=8, ep=8) for n in (381, 267, 999, 146)]
        batch = estimate_batch([(n, n) for n in (381, 267, 999, 146)], dp=8, ep=8)
        for name in ("dispatch_us", "combine_us"):
            difference = sum(row["components_us"][name] for row in singles) - batch["components_us"][name]
            self.assertAlmostEqual(difference, 3 * _reference().NPU["base_delay"])

    def test_saved_mixed_example(self):
        got = estimate_batch([(4096, 4096)] + [(512, 512)] * 3, dp=8, ep=8)
        self.assertAlmostEqual(got["total_ms"], 901.18525248, places=8)
        self.assertEqual(got["work"]["new_tokens"], 5632)
        self.assertEqual(got["work"]["indexer_pairs"], 8781824)
        self.assertEqual(got["work"]["exact_causal_pairs"], 8784640)

    def test_singletons_previous_numeric_result(self):
        times = [estimate_batch([(n, n)], dp=32, ep=32)["total_ms"]
                 for n in (4096, 512, 512, 512)]
        self.assertAlmostEqual(sum(times), 923.5142784, places=8)

    def test_same_new_tokens_still_preserves_attention_boundaries(self):
        mixed = estimate_batch([(4096, 4096)] + [(512, 512)] * 3, dp=8, ep=8)
        uniform = estimate_batch([(1408, 1408)] * 4, dp=8, ep=8)
        self.assertEqual(mixed["work"]["new_tokens"], uniform["work"]["new_tokens"])
        for name in ("indexer_q_us", "mla_projection_us", "dense_mlp_us", "moe_us"):
            self.assertEqual(mixed["components_us"][name], uniform["components_us"][name])
        self.assertGreater(mixed["components_us"]["indexer_score_us"],
                           uniform["components_us"]["indexer_score_us"])

    def test_prefix_bytes_are_separate_from_execution_and_not_padded(self):
        requests = [(10240, 2048), (513, 1), (256, 128)]
        got = estimate_batch(requests, dp=12, ep=12)
        self.assertEqual(got["prefix_kv_bytes"], (8192 + 512 + 128) * 1408 * 78)
        self.assertEqual(got["components_us"]["sparse_storage_us"], 0)
        self.assertEqual(got["work"]["kv_entries_per_read"], 10240 + 513 + 256)
        no_reuse = estimate_batch(requests, dp=12, ep=12, kv_hbm_reuse=False)
        self.assertEqual(no_reuse["work"]["kv_entries_per_read"], (2048 + 1 + 128) * 2048)
        fetched = estimate_batch(requests, dp=12, ep=12, sparse_HBM_hit_ratio=0.8)
        self.assertEqual(fetched["prefix_kv_bytes"], got["prefix_kv_bytes"])
        self.assertGreater(fetched["components_us"]["sparse_storage_us"], 0)
        self.assertAlmostEqual(fetched["total_us"] - got["total_us"],
                               78 * fetched["components_us"]["sparse_storage_us"])

    def test_deployment_does_not_shrink_with_batch(self):
        for bs in (1, 2, 4, 8):
            got = estimate_batch([(4096, 4096)] * bs, dp=12, ep=12)
            self.assertEqual(got["deployment"], dict(tp=1, dp=12, ep=12))

    def test_empty_and_zero_new_tokens_do_not_launch_kernels(self):
        self.assertEqual(per_batch([], dp=8, ep=8), (0, 0))
        empty_compute = estimate_batch([(512, 0)], dp=8, ep=8)
        self.assertEqual(empty_compute["total_us"], 0)
        self.assertEqual(empty_compute["prefix_kv_bytes"], 512 * 1408 * 78)
        self.assertTrue(all(t == 0 for t in empty_compute["components_us"].values()))
        self.assertEqual(empty_compute["moe_work"]["group_routed_token_expert_work"], 0)
        self.assertEqual(empty_compute["moe_work"]["group_expected_active_experts"], 0)
        for rank in empty_compute["moe_work"]["ranks"]:
            for name in ("routed_flops", "shared_flops", "routed_weight_bytes", "shared_weight_bytes",
                         "dispatch_logical_bytes", "combine_logical_bytes", "moe_us", "dispatch_us", "combine_us"):
                self.assertEqual(rank[name], 0, name)
        one = estimate_batch([(256, 256)], dp=8, ep=8)
        extra = estimate_batch([(256, 256), (512, 0)], dp=8, ep=8)
        self.assertEqual(extra["total_us"], one["total_us"])
        self.assertEqual(extra["batch_size"], 1)

    def test_units_and_layer_sum(self):
        rows = [(4352, 256), (512, 512)]
        got = estimate_batch(rows, dp=8, ep=8)
        per_layer, kv_gb = per_batch(rows, dp=8, ep=8)
        self.assertEqual(len(got["layer_durations_us"]), 78)
        self.assertEqual(got["layer_durations_us"][:3], [got["dense_layer_us"]] * 3)
        self.assertEqual(got["layer_durations_us"][3:], [got["moe_layer_us"]] * 75)
        self.assertAlmostEqual(per_layer * 78, got["total_us"])
        self.assertAlmostEqual(got["duration_s"] * 1000, got["total_ms"])
        self.assertAlmostEqual(kv_gb * 78 * 1e9, got["prefix_kv_bytes"])

    def test_permutation_and_generator_input(self):
        rows = [(16385, 1), (4608, 512), (4096, 4096)]
        a = estimate_batch(rows, dp=8, ep=8)
        b = estimate_batch(iter(reversed(rows)), dp=8, ep=8)
        self.assertEqual(a["work"], b["work"])
        self.assertEqual(a["components_us"], b["components_us"])

    def test_no_reference_or_input_mutation_with_concurrent_calls(self):
        ref = _reference()
        configs = copy.deepcopy((ref.MODEL, ref.NPU, ref.CONF))
        functions = (ref._nq, ref._kv_entries, ref._t_indexer_qk_score)
        rows = [[4352, 256], [512, 512]]
        before = copy.deepcopy(rows)
        expected = estimate_batch(rows, dp=8, ep=8)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: estimate_batch(rows, dp=8, ep=8), range(24)))
        self.assertTrue(all(result == expected for result in results))
        self.assertEqual(rows, before)
        self.assertEqual(configs, (ref.MODEL, ref.NPU, ref.CONF))
        self.assertEqual(functions, (ref._nq, ref._kv_entries, ref._t_indexer_qk_score))
        self.assertEqual(hashlib.sha256(REFERENCE.read_bytes()).hexdigest(), REFERENCE_SHA256)

    def test_invalid_inputs_fail_explicitly(self):
        for rows in ([(-1, 0)], [(4, 5)], [(4, -1)], [(4.0, 1)], [(4, True)],
                     [(131073, 1)], [(4,)], [None], [(4, 1)] * 9):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                per_batch(rows, dp=8, ep=8)
        for options in (dict(dp=0, ep=0), dict(dp=True, ep=1), dict(dp=8, ep=4),
                        dict(dp=1, ep=3, tp=3), dict(dp=257, ep=257),
                        dict(dp=8, ep=8, sparse_HBM_hit_ratio=float("nan")),
                        dict(dp=8, ep=8, sparse_HBM_hit_ratio=1.1),
                        dict(dp=8, ep=8, kv_hbm_reuse=1)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                per_batch([(4, 1)], **options)


if __name__ == "__main__":
    unittest.main()
