import unittest
from sim.storage import Storage, waterfill


def make_store(
    paths=16, bandwidth=100, latency=0, policy="active_split", disks=1, path_Bps=100
):
    return Storage(
        dict(
            disks=disks,
            paths_per_disk=paths,
            disk_Bps=100,
            shared_link_Bps=bandwidth,
            path_Bps=path_Bps,
            kv_bytes_per_token=100,
            read_latency_s=latency,
            bandwidth_policy=policy,
        ),
        1,
    )


def block_on_disk(s, disk, skip=()):
    for i in range(256):
        b = bytes([i])
        if s.disk_of(b) == disk and b not in skip:
            return b
    raise AssertionError("no block hashes to disk")


class WaterfillTests(unittest.TestCase):
    def test_surplus_split_equally(self):
        # 供过于求：按需分 + 剩余均分（设计文档 §3.4 例 1）
        a = waterfill(40, [2, 4, 8])
        for x, want in zip(a, [10 + 2 / 3, 12 + 2 / 3, 16 + 2 / 3]):
            self.assertAlmostEqual(x, want)
        self.assertAlmostEqual(sum(a), 40.0)

    def test_contended_iterates_to_fixed_point(self):
        # 供不应求：迭代到不动点（设计文档 §3.4 例 2）；单轮会得 [10,30,30,30]
        a = waterfill(100, [10, 28, 100, 100])
        for x, want in zip(a, [10, 28, 31, 31]):
            self.assertAlmostEqual(x, want)
        self.assertAlmostEqual(sum(a), 100.0)

    def test_uniform_demand_degenerates_to_equal_split(self):
        for total, n in ((24, 3), (60, 3), (400, 10)):  # 供过于求与不足两种情形
            a = waterfill(40, [total / n] * n)
            for x in a:
                self.assertAlmostEqual(x, 40 / n)

    def test_single_request_gets_whole_disk(self):
        self.assertAlmostEqual(waterfill(40, [8])[0], 40.0)
        self.assertAlmostEqual(waterfill(40, [80])[0], 40.0)  # d > 盘容量不预截断

    def test_exact_boundary(self):
        a = waterfill(40, [10, 30])
        self.assertAlmostEqual(a[0], 10.0)
        self.assertAlmostEqual(a[1], 30.0)


class StorageTests(unittest.TestCase):
    def test_dynamic_contention(self):
        s = make_store()
        a = b"a"
        b = b"b"
        s.present.update([a, b])
        s.submit(0, (0, "a"), [a])
        s.advance(0.5)  # a has transferred 50 of 100 bytes
        s.submit(0.5, (1, "b"), [b])
        self.assertAlmostEqual(s.next_time(), 1.5)
        self.assertEqual(s.advance(1.5), [(0, "a")])
        self.assertAlmostEqual(s.next_time(), 2.0)
        self.assertEqual(s.advance(2.0), [(1, "b")])
        self.assertAlmostEqual(s.bytes_transferred, 200.0)

    def test_same_path_fifo(self):
        s = make_store(paths=1)
        s.present.update([b"a", b"b"])
        s.submit(0, (0, "a"), [b"a"])
        s.submit(0, (1, "b"), [b"b"])
        self.assertEqual(s.advance(1), [(0, "a")])
        self.assertEqual(s.advance(2), [(1, "b")])

    def test_latency_and_link_cap(self):
        s = make_store(bandwidth=10, latency=0.2)
        s.present.add(b"a")
        s.submit(0, (0, "a"), [b"a"])
        self.assertEqual(s.rates(), {})
        self.assertAlmostEqual(s.next_time(), 0.2)
        self.assertEqual(s.advance(0.2), [])
        self.assertAlmostEqual(sum(s.rates().values()), 10.0)
        self.assertAlmostEqual(s.next_time(), 10.2)
        self.assertEqual(s.advance(10.2), [(0, "a")])

    def test_link_flux_demand_vs_actual(self):
        # 链路不绑定：actual == demand（盘内均分后的速率和）
        s = make_store(bandwidth=1000)
        blocks = [bytes([i]) for i in range(4)]
        s.present.update(blocks)
        s.submit(0, (0, "a"), blocks)  # 最少占用指派铺到 4 条路径 → demand=4×25
        s.advance(0)
        demand, actual = s.link_flux()
        self.assertGreater(demand, 0)
        self.assertEqual(actual, demand)
        # 链路绑定：actual 被压到 shared_link_Bps，demand 不变
        s2 = make_store(bandwidth=10)
        s2.present.update(blocks)
        s2.submit(0, (0, "a"), blocks)
        s2.advance(0)
        d2, a2 = s2.link_flux()
        self.assertAlmostEqual(a2, 10.0)
        # 与 rates() 一致（actual = Σrates）；区间守恒：∫actual = bytes
        self.assertAlmostEqual(a2, sum(s2.rates().values()))
        total = 0.0
        t = s2.now
        while s2.pending:  # 读全部完成后 pending 清空（queues 只留空 deque 键）
            demand, actual = s2.link_flux()
            nxt = s2.next_time()
            total += actual * (nxt - t)
            done = s2.advance(nxt)
            t = nxt
        self.assertAlmostEqual(total, s2.bytes_transferred, places=6)

    def test_active_split_dispatch_spreads(self):
        # 发 IO 时按最少占用指派：4 块铺满 4 条不同 Path，第 5 块落在最短的队上
        s = make_store(paths=4)
        blocks = [bytes([i]) for i in range(5)]
        s.present.update(blocks)
        s.submit(0, (0, "a"), blocks)
        lens = sorted(len(q) for q in s.queues.values())
        self.assertEqual(lens, [1, 1, 1, 2])
        self.assertEqual(sum(s.rates().values()), 100.0)  # 4 活跃队首均分整盘

    def test_dispatch_deterministic(self):
        def paths_of():
            s = make_store(paths=8)
            blocks = [bytes([i]) for i in range(6)]
            s.present.update(blocks)
            s.submit(0, (0, "a"), blocks)
            return sorted(k for k, q in s.queues.items() if q)

        self.assertEqual(paths_of(), paths_of())

    def test_bad_policy_rejected(self):
        with self.assertRaises(ValueError):
            make_store(policy="bogus")

    def test_max_min_fair_rates(self):
        # 供过于求：[8,16] → [46,54]（按需 + 剩余 76 均分）；供不应求：[30,100] → [30,70]
        s = make_store(policy="max_min_fair")
        s.present.update([b"a", b"b"])
        s.submit(0, (0, "a"), [b"a"], 8)
        s.submit(0, (1, "b"), [b"b"], 16)
        r = s.rates()
        self.assertAlmostEqual(r[(0, 0)], 46.0)
        self.assertAlmostEqual(r[(0, 1)], 54.0)
        s2 = make_store(policy="max_min_fair")
        s2.present.update([b"a", b"b"])
        s2.submit(0, (0, "a"), [b"a"], 30)
        s2.submit(0, (1, "b"), [b"b"], 100)
        r2 = s2.rates()
        self.assertAlmostEqual(r2[(0, 0)], 30.0)
        self.assertAlmostEqual(r2[(0, 1)], 70.0)

    def test_max_min_fair_no_queueing_beyond_paths(self):
        # Path 数不设上限：超过 paths_per_disk 的读请求仍然全部活跃
        s = make_store(paths=4, policy="max_min_fair")
        blocks = [bytes([i]) for i in range(10)]
        s.present.update(blocks)
        s.submit(0, (0, "a"), blocks, 30)
        self.assertEqual(len(s.rates()), 10)  # 每个读请求独占一条 Path
        self.assertAlmostEqual(sum(s.rates().values()), 100.0)

    def test_max_min_fair_requires_demand(self):
        s = make_store(policy="max_min_fair")
        s.present.add(b"a")
        with self.assertRaises(AssertionError):
            s.submit(0, (0, "a"), [b"a"])

    def test_max_min_fair_single_read_gets_whole_disk(self):
        s = make_store(policy="max_min_fair", bandwidth=1000)
        s.present.add(b"a")
        s.submit(0, (0, "a"), [b"a"], 8)
        self.assertAlmostEqual(list(s.rates().values())[0], 100.0)

    def test_max_min_fair_timeline_analytic(self):
        # 两个 100 字节读、需求各 30、盘 100：各 50 → t=2 同时完成；
        # 与 active_split 的差别在于完成后独占者拿整盘（无 path_Bps 封顶）。
        s = make_store(policy="max_min_fair", bandwidth=1000)
        s.present.update([b"a", b"b"])
        s.submit(0, (0, "a"), [b"a"], 30)
        s.submit(0, (1, "b"), [b"b"], 30)
        self.assertAlmostEqual(s.next_time(), 2.0)
        done = s.advance(2.0)
        self.assertEqual(sorted(done), [(0, "a"), (1, "b")])
        self.assertAlmostEqual(s.bytes_transferred, 200.0)

    def test_max_min_fair_link_flux_and_conservation(self):
        s = make_store(bandwidth=10, policy="max_min_fair")
        blocks = [bytes([i]) for i in range(4)]
        s.present.update(blocks)
        s.submit(0, (0, "a"), blocks, 30)
        s.advance(0)
        demand, actual = s.link_flux()
        self.assertAlmostEqual(demand, 100.0)  # 盘内水土填充满整盘
        self.assertAlmostEqual(actual, 10.0)
        total = 0.0
        t = s.now
        while s.pending:
            _, actual = s.link_flux()
            nxt = s.next_time()
            total += actual * (nxt - t)
            s.advance(nxt)
            t = nxt
        self.assertAlmostEqual(total, s.bytes_transferred, places=6)

    def test_per_disk_isolation(self):
        # 两盘各自水土填充，互不影响；链路对两盘总和压缩
        s = make_store(disks=2, policy="max_min_fair", bandwidth=1000)
        a = block_on_disk(s, 0)
        b = block_on_disk(s, 1, skip=(a,))
        s.present.update([a, b])
        s.submit(0, (0, "a"), [a], 8)
        s.submit(0, (1, "b"), [b], 16)
        r = s.rates()
        self.assertAlmostEqual(r[(0, 0)], 100.0)  # 盘 0 独占者拿整盘
        self.assertAlmostEqual(r[(1, 0)], 100.0)


if __name__ == "__main__":
    unittest.main()
