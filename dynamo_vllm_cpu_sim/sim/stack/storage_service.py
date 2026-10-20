"""storage-sim 服务进程（W5）：把单进程 DES 的 Storage 对象服务化。

复用 sim/storage.py 的 Storage 原样（两带宽策略、Path 发放时指派、共享链路
压缩语义不变）。时间推进：壁钟模式（设计文档模式 1）——虚拟 now 取服务进程
的墙钟纪元秒，惰性推进（每次 RPC 到来时把内部事件队列推进到当前墙钟，
逐步停在 Storage.next_time() 的事件边界上，与 DES run.py 的事件循环同构，
只是触发点从"全局事件循环"换成"RPC 到达"）。

共享池预置：与 DES run.py:52-58 相同——用真实 Request 的 block_hashes 按
trace 的 remote_prefix_tokens 装载（读侧池，t=0 快照语义）。

用法::

    .venv/bin/python -m sim.stack.storage_service \
        --config configs/demo.json --trace configs/stack_remote.jsonl \
        --socket /tmp/stack_logs/stack/storage.sock
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("VLLM_PLUGINS", "")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sim.engine import make_request  # noqa: E402
from sim.storage import Storage  # noqa: E402


class Service:
    def __init__(self, config: dict, trace_rows: list[dict]):
        self.store = Storage(config["storage"], config["block_size"])
        self.block_size = config["block_size"]
        reqs = {
            r["id"]: make_request(r, config["block_size"], config.get("policy", "fcfs"))
            for r in trace_rows
        }
        for r in trace_rows:
            self.store.present.update(
                reqs[r["id"]].block_hashes[
                    : r.get("remote_prefix_tokens", 0) // self.block_size
                ]
            )
        self.store.now = 0.0
        # 虚拟时间 = 服务启动后流逝的墙钟秒（不直接用纪元秒：1.8e9 量级下
        # double 分辨率 ~0.4µs，会在 Storage.advance 的剩余字节扣减上死循环；
        # 流逝秒回到 DES 同量级，精度 ~1e-16s）。store.now 语义即"此刻已流
        # 逝秒数"，与服务内所有读的完成时间同基准。
        self.t0 = time.time()
        # 每 worker 的已完成未领取读集合
        self._finished: dict[int, list[str]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _drain_done(self, done: list[tuple[int, str]]) -> None:
        for worker, rid in done:
            self._finished.setdefault(worker, []).append(rid)

    def _advance_to_wall(self) -> None:
        now = time.time() - self.t0
        # 先逐步停在事件边界（advance 要求 target ≤ next_time），再推进到当前。
        # 步数保险：正常单次推进的事件数远小于此；越界说明推进语义被破坏。
        for _ in range(100_000):
            nxt = self.store.next_time()
            if nxt > now + 1e-9:
                break
            self._drain_done(self.store.advance(nxt))
        else:
            raise RuntimeError("advance guard: >100k storage events in one poll")
        if now > self.store.now + 1e-12:
            self._drain_done(self.store.advance(now))

    # ------------------------------------------------------------------
    def op_present(self, blocks_hex: list[str]) -> dict:
        return {
            "ok": True,
            "present": [bytes.fromhex(h) in self.store.present for h in blocks_hex],
        }

    def op_submit(self, worker: int, rid: str, blocks_hex: list[str], demand) -> dict:
        self._advance_to_wall()
        blocks = [bytes.fromhex(h) for h in blocks_hex]
        self.store.submit(self.store.now, (worker, rid), blocks, demand)
        return {"ok": True}

    def op_finished(self, worker: int) -> dict:
        self._advance_to_wall()
        rids = self._finished.pop(worker, [])
        return {"ok": True, "rids": rids}

    def op_stats(self) -> dict:
        snap = self.store.snapshot()
        snap["ok"] = True
        snap["pending_reads"] = len(self.store.pending)
        snap["finished_unclaimed"] = {w: len(v) for w, v in self._finished.items()}
        return snap


def handle(conn: socket.socket, service: Service) -> None:
    buf = b""
    while True:
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        line, buf = buf.split(b"\n", 1)
        req = json.loads(line)
        with service._lock:
            try:
                op = req.get("op")
                if op == "present":
                    resp = service.op_present(req.get("blocks_hex", []))
                elif op == "submit":
                    resp = service.op_submit(
                        req.get("worker"),
                        req.get("rid"),
                        req.get("blocks_hex", []),
                        req.get("demand"),
                    )
                elif op == "finished":
                    resp = service.op_finished(req.get("worker"))
                elif op == "stats":
                    resp = service.op_stats()
                else:
                    resp = {"ok": False, "error": f"unknown op {op!r}"}
            except Exception as exc:  # noqa: BLE001
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        conn.sendall((json.dumps(resp) + "\n").encode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/demo.json")
    ap.add_argument("--trace", required=True, help="trace JSONL（用于预置共享池）")
    ap.add_argument("--socket", required=True)
    ap.add_argument("--bandwidth-policy", choices=["active_split", "max_min_fair"])
    a = ap.parse_args()
    cfg = json.loads((ROOT / a.config).read_text())
    if a.bandwidth_policy:
        cfg["storage"]["bandwidth_policy"] = a.bandwidth_policy
    rows = [
        json.loads(x)
        for x in (ROOT / a.trace).read_text().splitlines()
        if x.strip()
    ]
    service = Service(cfg, rows)
    path = Path(a.socket)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(16)
    print(
        json.dumps(
            {
                "storage_service": "listening",
                "socket": str(path),
                "pool_blocks": len(service.store.present),
                "bandwidth_policy": service.store.policy,
                "t0_epoch_s": service.t0,
            }
        ),
        flush=True,
    )
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, service), daemon=True).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
