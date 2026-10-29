"""storage-sim 服务进程（W5）：把单进程 DES 的 Storage 对象服务化。

复用 sim/storage.py 的 Storage 原样（两带宽策略、Path 发放时指派、共享链路
压缩语义不变）。时间推进：壁钟模式（设计文档模式 1）——虚拟 now 取服务启动后的
单调流逝秒，惰性推进（每次 RPC 到来时把内部事件队列推进到当前单调时刻，
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
from sim.mooncake import engine_key, validate_hit_row  # noqa: E402


class Service:
    def __init__(self, config: dict, trace_rows: list[dict]):
        self.store = Storage(config["storage"], config["block_size"])
        self.block_size = config["block_size"]
        self.first_seen = config.get("cache_model") == "first_seen"
        self.trace_by_id = {r["id"]:r for r in trace_rows}
        if len(self.trace_by_id) != len(trace_rows):
            raise ValueError("duplicate trace request id")
        self.hit_bindings, self.hit_queries, self._matched = {}, [], {}
        if self.first_seen:
            if config.get("enable_prefix_caching",False):
                raise ValueError("first_seen requires local prefix caching disabled")
            for r in trace_rows:
                validate_hit_row(r,self.block_size)
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
        self._trace_hashes = {rid:list(req.block_hashes) for rid,req in reqs.items()} if self.first_seen else {}
        self.store.now = 0.0
        # 虚拟时间 = 服务启动后流逝的墙钟秒（不直接用纪元秒：1.8e9 量级下
        # double 分辨率 ~0.4µs，会在 Storage.advance 的剩余字节扣减上死循环；
        # 流逝秒回到 DES 同量级，精度 ~1e-16s）。store.now 语义即"此刻已流
        # 逝秒数"，与服务内所有读的完成时间同基准。
        # WSL/宿主校时可以跳变 time.time；资源推进必须使用单调时钟。
        self.t0 = time.monotonic()
        self.t0_epoch = time.time()
        # 每 worker 的已完成未领取读集合
        self._finished: dict[int, list[str]] = {}
        self._lock = threading.Lock()
        self.peak_pending_requests = 0
        self.bytes_submitted = 0
        self.first_submit_s = None
        self.last_completion_s = None
        self.record_reads = config["storage"].get("record_reads", False)
        self.read_history = []
        self._read_records = {}

    # ------------------------------------------------------------------
    def _drain_done(self, done: list[tuple[int, str]]) -> None:
        for worker, rid in done:
            self.last_completion_s = self.store.now
            record = self._read_records.pop((worker, rid), None)
            if record is not None:
                record["completed_s"] = self.store.now
            self._finished.setdefault(worker, []).append(rid)

    def _advance_to_wall(self) -> None:
        now = time.monotonic() - self.t0
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
        if self.first_seen:
            raise ValueError("first_seen requires a bound per-request hit query")
        return {
            "ok": True,
            "present": [bytes.fromhex(h) in self.store.present for h in blocks_hex],
        }

    def op_bind(self, engine_id, source_id, prompt_token_ids, hit_tokens):
        """转发前绑定离线输入，不能从物理池扩大请求的命中长度。"""
        if not self.first_seen:
            raise ValueError("bind is only valid for first_seen")
        row = self.trace_by_id[source_id]
        if row["prompt_token_ids"] != prompt_token_ids or row["hit_tokens"] != hit_tokens:
            raise ValueError("HTTP prompt/hit differs from frozen trace contract")
        key = engine_key(engine_id)
        if key in self.hit_bindings or source_id in self.hit_bindings.values():
            raise ValueError("duplicate first_seen binding")
        self.hit_bindings[key] = source_id
        return dict(ok=True)

    def op_present_for_request(self, rid, start_block, blocks_hex):
        source_id = self.hit_bindings[engine_key(rid)]
        row = self.trace_by_id[source_id]
        expected = self._trace_hashes[source_id]
        blocks = [bytes.fromhex(h) for h in blocks_hex]
        if (type(start_block) is not int or start_block < 0
                or start_block+len(blocks) > len(expected)
                or expected[start_block:start_block+len(blocks)] != blocks):
            raise ValueError("query blocks differ from bound prompt")
        limit = row["hit_tokens"]//self.block_size
        present = [start_block+i < limit and b in self.store.present for i,b in enumerate(blocks)]
        matched = []
        for b,hit in zip(blocks,present):
            if not hit:
                break
            matched.append(b)
        self._matched[rid] = matched
        self.hit_queries.append(dict(rid=rid,source_id=source_id,start_block=start_block,
            candidate_blocks=len(blocks),matched_tokens=len(matched)*self.block_size,
            hit_tokens=row["hit_tokens"]))
        return dict(ok=True,present=present)

    def op_submit(self, worker: int, rid: str, blocks_hex: list[str], demand) -> dict:
        self._advance_to_wall()
        blocks = [bytes.fromhex(h) for h in blocks_hex]
        if self.first_seen and (rid not in self._matched or blocks != self._matched[rid]):
            raise ValueError("read does not match the frozen request hit contract")
        before = self.store.disk_bytes_submitted.copy() if self.record_reads else None
        self.store.submit(self.store.now, (worker, rid), blocks, demand)
        if self.record_reads:
            # op_submit runs under the service lock. Counter deltas therefore
            # describe exactly this read, including repeated blocks, without a
            # second placement/hash pass over its blocks.
            by_disk = [after - prior for after, prior in
                       zip(self.store.disk_bytes_submitted, before)]
            record = dict(worker=worker, rid=rid, submitted_s=self.store.now,
                          completed_s=None, blocks_hex=blocks_hex,
                          disk_bytes=by_disk, per_block_demand_Bps=demand)
            self.read_history.append(record)
            self._read_records[(worker, rid)] = record
        self.bytes_submitted += len(blocks)*self.store.block_bytes
        if self.first_submit_s is None:
            self.first_submit_s = self.store.now
        self.peak_pending_requests = max(self.peak_pending_requests,len(self.store.pending))
        return {"ok": True}

    def op_finished(self, worker: int) -> dict:
        self._advance_to_wall()
        rids = self._finished.pop(worker, [])
        return {"ok": True, "rids": rids}

    def op_stats(self) -> dict:
        self._advance_to_wall()
        snap = self.store.snapshot()
        snap["ok"] = True
        snap["pending_reads"] = len(self.store.pending)
        snap["peak_pending_requests"] = self.peak_pending_requests
        snap["bytes_submitted"] = self.bytes_submitted
        snap["bytes_transferred"] = self.store.bytes_transferred
        snap["first_submit_s"] = self.first_submit_s
        snap["last_completion_s"] = self.last_completion_s
        snap["finished_unclaimed"] = {w: len(v) for w, v in self._finished.items()}
        if self.first_seen:
            snap.update(cache_model="first_seen",hit_bindings=self.hit_bindings,hit_queries=self.hit_queries)
        if self.record_reads:
            snap["read_history"] = self.read_history
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
                elif op == "bind":
                    resp = service.op_bind(req["engine_id"],req["source_id"],req["prompt_token_ids"],req["hit_tokens"])
                elif op == "present_for_request":
                    resp = service.op_present_for_request(req["rid"],req["start_block"],req["blocks_hex"])
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
                "disks": service.store.c["disks"],
                "disk_Bps": service.store.c["disk_Bps"],
                "shared_link_Bps": service.store.link_Bps,
                "placement": "hrw_sha256_content_hash",
                "t0_epoch_s": service.t0_epoch,
                "t0_monotonic_s": service.t0,
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
