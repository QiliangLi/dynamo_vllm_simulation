"""A/B trace 生成器的不变量测试（docs/AB双类带宽争抢实验设计-20261008.md §6）。

覆盖：顺序/计数/到达时刻、通过 sim.run.validate、每类读字节命中大纲目标
（A 1.4052GB、B 0.30798GB，容差 0.01%）、生成确定性、请求间前缀互异。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gen_ab_trace import A_CLASS, B_CLASS, gen_rows  # noqa: E402

KV_BYTES_PER_TOKEN = 10741.744
TARGET_READ_GB = {"A": 1.4052, "B": 0.30798}


def cls(row):
    return row["id"].rstrip("0123456789s")[-1:].upper()


class TestOrderAndCounts(unittest.TestCase):
    def test_block_order(self):
        rows = gen_rows("block", rounds=2, burst_s=1.0)
        self.assertEqual(len(rows), 192)
        for r in (0, 1):
            chunk = [x for x in rows if x["arrival_s"] == float(r)]
            self.assertEqual(len(chunk), 96)
            seq = [cls(x) for x in chunk]  # validate 已按 (arrival,id) 排序
            self.assertEqual(seq, ["A"] * 32 + ["B"] * 64)

    def test_interleave_order(self):
        rows = gen_rows("interleave", rounds=1)
        seq = [cls(x) for x in rows]
        self.assertEqual(seq, ["A", "B", "B"] * 32)

    def test_class_params(self):
        rows = gen_rows("block", rounds=1)
        a = next(x for x in rows if cls(x) == "A")
        b = next(x for x in rows if cls(x) == "B")
        self.assertEqual(len(a["prompt_token_ids"]), sum(A_CLASS[:2]))
        self.assertEqual(a["remote_prefix_tokens"], A_CLASS[0])
        self.assertEqual(len(b["prompt_token_ids"]), sum(B_CLASS[:2]))
        self.assertEqual(b["remote_prefix_tokens"], B_CLASS[0])


class TestReadBytes(unittest.TestCase):
    def test_per_class_read_volume(self):
        rows = gen_rows("interleave", rounds=1)
        for c, target in TARGET_READ_GB.items():
            per_class = [x for x in rows if cls(x) == c]
            got = sum(x["remote_prefix_tokens"] for x in per_class) / len(per_class)
            gb = got * KV_BYTES_PER_TOKEN / 1e9
            self.assertAlmostEqual(gb / target, 1.0, places=4, msg=c)


class TestValidityAndDeterminism(unittest.TestCase):
    def test_rows_pass_sim_validate(self):
        from sim.run import validate

        c = json.loads(
            (Path(__file__).resolve().parents[1] / "configs" / "ab.json").read_text()
        )
        for order in ("block", "interleave"):
            validate(gen_rows(order, rounds=1), c)

    def test_deterministic(self):
        self.assertEqual(gen_rows("block", rounds=1), gen_rows("block", rounds=1))
        self.assertEqual(
            json.dumps(gen_rows("interleave", rounds=1), sort_keys=True),
            json.dumps(gen_rows("interleave", rounds=1), sort_keys=True),
        )

    def test_unique_prefixes(self):
        rows = gen_rows("block", rounds=2)
        heads = {tuple(x["prompt_token_ids"][:16]) for x in rows}
        self.assertEqual(len(heads), len(rows))
        # 全零/常数序列会被前缀缓存互吃，确认 token 确实随 ordinal 变化
        self.assertGreater(len({x["prompt_token_ids"][0] for x in rows}), 1)


if __name__ == "__main__":
    unittest.main()
