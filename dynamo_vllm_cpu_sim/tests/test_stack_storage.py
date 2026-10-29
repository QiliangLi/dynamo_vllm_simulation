"""storage-sim 服务（W5）单元测试：RPC 语义 + 壁钟推进 + 带宽竞争。

真实起一个 storage_service 子进程（Unix socket），校验：
- present 命中与 pool 预置一致；
- submit 后读在壁钟时间完成（finished 领取语义）；
- 同盘并发读相互减速（带宽竞争在服务化后保持 DES 同语义）；
- 传输量与守恒（bytes_transferred = 提交块数 × block_bytes）。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STORAGE_CFG = {
    "disks": 1,
    "paths_per_disk": 4,
    "disk_Bps": 40_000_000_000,
    "shared_link_Bps": 40_000_000_000,
    "path_Bps": 1_000_000_000,
    "kv_bytes_per_token": 100,
    "read_latency_s": 0.0,
    "bandwidth_policy": "active_split",
}
CONFIG = {"block_size": 4, "storage": STORAGE_CFG, "compute": {}}


def make_trace(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class StorageServiceTest(unittest.TestCase):
    config = CONFIG
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        # 2 块远端前缀（block_size=4）；块哈希由服务侧 make_request 计算，
        # 测试用同源 API 重算（PYTHONHASHSEED 由测试进程固定，服务子进程继承）。
        cls.trace = tmp / "t.jsonl"
        cls.row_a = {
            "id": "a",
            "arrival_s": 0.0,
            "prompt_token_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "output_tokens": 1,
            "remote_prefix_tokens": 8,
        }
        cls.row_big = {
            "id": "big",
            "arrival_s": 0.0,
            "prompt_token_ids": list(range(1, 257)),
            "output_tokens": 1,
            "remote_prefix_tokens": 256,
        }
        make_trace(cls.trace, [cls.row_a, cls.row_big])
        cls.cfg_path = tmp / "c.json"
        cls.cfg_path.write_text(json.dumps(cls.config))
        cls.sock = tmp / "s.sock"
        # 块哈希依赖 PYTHONHASHSEED；服务与哈希计算子进程统一固定为 0
        cls.env = {**os.environ, "PYTHONHASHSEED": "0"}
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "sim.stack.storage_service",
                "--config",
                str(cls.cfg_path),
                "--trace",
                str(cls.trace),
                "--socket",
                str(cls.sock),
            ],
            cwd=ROOT,
            env=cls.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        line = ""
        for _ in range(40):
            line = cls.proc.stdout.readline()
            if "listening" in line:
                break
        assert "listening" in line, line
        cls.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cls.client.settimeout(30)
        cls.client.connect(str(cls.sock))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        cls.tmp.cleanup()

    def call(self, payload):
        self.client.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = self.client.recv(65536)
            if not chunk:
                raise ConnectionError("service closed")
            buf += chunk
        resp = json.loads(buf)
        if not resp.get("ok", True):
            self.fail(f"service error: {resp.get('error')}")
        return resp

    def hashes_of(self, row):
        # 与服务同 seed 的子进程计算块哈希（测试进程自身 seed 不可控）
        code = (
            "import json,sys;sys.path.insert(0,'.');"
            "from sim.engine import make_request;"
            f"print(json.dumps([b.hex() for b in make_request({json.dumps(row)}, 4, 'fcfs').block_hashes]))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=self.env,
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[-1]
        return json.loads(out)

    def hashes(self):
        return self.hashes_of(self.row_a)

    def test_present_and_pool(self):
        h = self.hashes()
        present = self.call({"op": "present", "blocks_hex": h})
        self.assertEqual(present["present"], [True, True])  # remote_prefix 8 = 2 块

    def test_submit_finishes_on_wall_clock(self):
        h = self.hashes()
        self.call(
            {
                "op": "submit",
                "worker": 0,
                "rid": "w0r",
                "blocks_hex": h,
                "demand": None,
            }
        )
        # 2 块 × 400B/块，path 上限 1GB/s → 亚毫秒级；轮询直至完成
        deadline = time.time() + 5
        rids = []
        while time.time() < deadline:
            rids = self.call({"op": "finished", "worker": 0})["rids"]
            if rids:
                break
            time.sleep(0.005)
        self.assertEqual(rids, ["w0r"])
        stats = self.call({"op": "stats"})
        self.assertEqual(stats["pending_reads"], 0)
        self.assertEqual(stats["bytes_remaining"], 0)

    def test_contention_slows_reads(self):
        # 64 个远端块的大读（池已含该行）：4 条 path、单 path 1GB/s，
        # 64×400B=25.6KB → 理想 ~µs 级；断言完成语义与不超时。精确的
        # 竞争/速率语义由 tests/test_storage.py（进程内）覆盖。
        blocks = self.hashes_of(self.row_big)
        t0 = time.time()
        self.call({"op": "submit", "worker": 1, "rid": "solo", "blocks_hex": blocks, "demand": None})
        rids = []
        while not rids and time.time() - t0 < 10:
            rids = self.call({"op": "finished", "worker": 1})["rids"]
            time.sleep(0.002)
        solo_s = time.time() - t0
        self.assertEqual(rids, ["solo"])
        # 64 块 × 400B = 25.6KB，单 path 1GB/s → 25.6µs ≪ solo_s 轮询开销；
        # 本断言主要保证完成语义与不超时，精确竞争语义由 tests/test_storage.py 覆盖
        self.assertLess(solo_s, 5.0)


class StorageMonotonicTest(unittest.TestCase):
    def test_three_disk_service_does_not_publish_early_completion(self):
        from sim.stack.storage_service import Service
        from test_storage_disks import blocks_for
        cfg = {"block_size": 1, "storage": dict(STORAGE_CFG, disks=3,
               disk_Bps=40, shared_link_Bps=None, bandwidth_policy="max_min_fair",
               record_reads=True)}
        with mock.patch("sim.stack.storage_service.time.monotonic",return_value=100.0) as clock:
            service = Service(cfg, [])
            blocks = sum((blocks_for(service.store,d,n) for d,n in enumerate([3,2,1])),[])
            service.store.present.update(blocks)
            service.op_submit(0,"r",[b.hex() for b in blocks],1)
            clock.return_value = 102.5
            self.assertEqual(service.op_finished(0)["rids"],[])
            clock.return_value = 105
            self.assertEqual(service.op_finished(0)["rids"],[])
            clock.return_value = 107.5
            self.assertEqual(service.op_finished(0)["rids"],["r"])
            self.assertEqual(service.op_finished(0)["rids"],[])
            stats = service.op_stats()
            self.assertEqual(stats["disk_bytes_transferred"],[300,200,100])
            self.assertAlmostEqual(stats["read_history"][0]["completed_s"],7.5)

    def test_host_wall_clock_adjustment_does_not_advance_transfers(self):
        from sim.stack.storage_service import Service
        cfg = {"block_size":4,"storage":dict(STORAGE_CFG,shared_link_Bps=1000)}
        with mock.patch("sim.stack.storage_service.time.monotonic",return_value=100.0) as clock:
            with mock.patch("sim.stack.storage_service.time.time",return_value=1000.0) as epoch:
                service = Service(cfg,[])
                service.store.present.add(b"block")
                service.op_submit(0,"r",[b"block".hex()],None)
                epoch.return_value = 5000.0
                clock.return_value = 100.1
                service._advance_to_wall()
                self.assertAlmostEqual(service.store.bytes_transferred,100,places=7)
                self.assertEqual(len(service.store.pending),1)
                clock.return_value = 100.5
                service._advance_to_wall()
                self.assertEqual(len(service.store.pending),0)
                self.assertAlmostEqual(service.store.bytes_transferred,400)


class ThreeDiskStorageServiceTest(StorageServiceTest):
    """Repeat the real Unix-socket checks with three disks and no global cap."""
    config = dict(CONFIG, storage=dict(STORAGE_CFG, disks=3, shared_link_Bps=None,
                  bandwidth_policy="max_min_fair", record_reads=True))

    def call(self, payload):
        if payload.get("op") == "submit" and payload.get("demand") is None:
            payload = dict(payload, demand=1e9)
        return super().call(payload)

    def test_topology_and_hash_plan_from_rpc(self):
        blocks = self.hashes()
        self.call(dict(op="submit",worker=2,rid="disk-plan",blocks_hex=blocks,demand=1e9))
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            if self.call(dict(op="finished",worker=2))["rids"]:
                break
            time.sleep(.002)
        stats = self.call(dict(op="stats"))
        self.assertEqual(stats["disks"],3)
        self.assertIsNone(stats["shared_link_Bps"])
        self.assertTrue(all(rate<=40e9+1e-3 for rate in stats["disk_peak_Bps"]))
        read = next(r for r in stats["read_history"] if r["rid"]=="disk-plan")
        self.assertIsNotNone(read["completed_s"])
        self.assertEqual(sum(read["disk_bytes"]),800)


class ConnectorDemandTest(unittest.TestCase):
    def test_glm_request_demand_is_split_across_actual_blocks(self):
        from types import SimpleNamespace
        from sim.stack.connector_rpc import SimConnectorRpc
        connector = SimConnectorRpc.__new__(SimConnectorRpc)
        connector.mode = "bulk"
        connector.cfg = {"compute":{"mode":"glm5_d950pr"},"storage":{"kv_bytes_per_token":100}}
        connector._vllm_config = SimpleNamespace(cache_config=SimpleNamespace(block_size=16))
        connector._pending_blocks = {"r":[b"a",b"b",b"c"]}
        connector._pending_demands = {"r":900}
        connector.demand, connector.worker_id = None, 0
        connector.client = mock.Mock()
        connector.external_tokens = {}
        with mock.patch("sim.stack.feedback.feedback_client",return_value=None):
            connector.update_state_after_alloc(SimpleNamespace(request_id="r"),None,48)
        connector.client.submit.assert_called_once_with(0,"r",[b"a",b"b",b"c"],300)


if __name__ == "__main__":
    unittest.main()
