"""SA 源解析锚点、在途重配、到期升速与字节守恒。"""

import json
from pathlib import Path
import unittest

from sim.sa_model import LayerStorage, layer_compute, reference

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "configs/sa_e26f_profile.json").read_text())


class SAProfileTests(unittest.TestCase):
    def test_source_calibration_and_zero_read_reference(self):
        for h, u, c in ((42883, 256, 0.00602), (9399, 4096, 0.02859)):
            self.assertAlmostEqual(layer_compute([(h, u)], PROFILE), c, places=12)
        for u in (2657, 8110):
            v, c, t0 = reference({"h_tokens": 0, "u_tokens": u}, PROFILE, 120e9, 200e9)
            self.assertEqual(v, 0)
            self.assertEqual(t0, 8 * c)

    def test_batch_eta_uses_summed_real_tokens_and_attention(self):
        # 两个短请求组合后摊薄 launch 与 eta 地板，不能按单请求时间相加。
        singleton = layer_compute([(42883, 256)], PROFILE)
        batched = layer_compute([(42883, 256)] * 2, PROFILE)
        self.assertLess(batched, 2 * singleton)
        self.assertAlmostEqual(
            batched,
            float(1 / 20000)
            + float(13408301 / 8239718400000) * 512
            + float(961 / 2059929600000) * 2 * (256 * 42883 + 256 * 257 // 2),
            places=12,
        )


class SAStorageTests(unittest.TestCase):
    def test_new_flow_reallocates_all_inflight(self):
        st = LayerStorage(40, 200)
        st.submit((0, 0, 0), 40, 30)
        st.allocate()
        self.assertEqual(st.flows[(0, 0, 0)].rate, 40)
        st.advance(0.25)
        st.submit((1, 0, 0), 20, 20)
        st.allocate()
        self.assertEqual([f.rate for f in st.flows.values()], [20, 20])
        self.assertEqual(st.next_time(), 1.25)
        done = st.advance(1.25)
        self.assertEqual(done, [(1, 0, 0)])
        st.allocate()
        self.assertEqual(st.flows[(0, 0, 0)].rate, 40)
        st.advance(st.next_time())
        self.assertFalse(st.flows)
        self.assertEqual(st.bytes_transferred, 60)

    def test_due_boost_and_bonus_match_sa(self):
        st = LayerStorage(120, 200)
        st.submit((0, 0, 0), 100, 5, 0.1)
        st.submit((1, 0, 0), 100, 200)
        st.allocate()
        self.assertEqual([f.rate for f in st.flows.values()], [5, 115])
        self.assertEqual(st.next_time(), 0.1)
        st.advance(0.1)
        st.allocate()
        self.assertEqual([f.rate for f in st.flows.values()], [60, 60])
        bonus = LayerStorage(120, 200)
        bonus.submit((0, 0, 0), 1, 5)
        bonus.submit((1, 0, 0), 1, 15)
        bonus.allocate()
        self.assertEqual([f.rate for f in bonus.flows.values()], [55, 65])

    def test_fcfs_is_not_max_min(self):
        st = LayerStorage(40, 200, alloc="fcfs")
        st.submit((0, 0, 0), 100, 30)
        st.submit((1, 0, 0), 100, 20)
        st.allocate()
        self.assertEqual([f.rate for f in st.flows.values()], [30, 10])


if __name__ == "__main__":
    unittest.main()
