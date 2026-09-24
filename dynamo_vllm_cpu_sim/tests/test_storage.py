import unittest
from sim.storage import Storage


def make_store(paths=16, bandwidth=100, latency=0):
    return Storage(
        dict(
            disks=1,
            paths_per_disk=paths,
            disk_Bps=100,
            shared_link_Bps=bandwidth,
            path_Bps=100,
            kv_bytes_per_token=100,
            read_latency_s=latency,
        ),
        1,
    )


class StorageTests(unittest.TestCase):
    def test_dynamic_contention(self):
        s = make_store()
        a = b"a"
        b = next(
            bytes([i]) for i in range(256) if s.location(bytes([i])) != s.location(a)
        )
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


if __name__ == "__main__":
    unittest.main()
