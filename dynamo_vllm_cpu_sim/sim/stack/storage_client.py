"""storage-sim 的 JSON-lines Unix-domain-socket 客户端（W5）。

协议（每行一个 JSON 请求，一行一个 JSON 响应）：
  {"op":"present","blocks_hex":[...]}        -> {"present":[true,false,...]}
  {"op":"submit","worker":0,"rid":"r0","blocks_hex":[...],"demand":123.0}
                                             -> {"ok":true}
  {"op":"finished","worker":0}               -> {"rids":["r0",...]}
  {"op":"stats"}                             -> Storage.snapshot() + pending 数

block hash 跨进程以 hex 传输（BlockHash = NewType(bytes)）。
"""

from __future__ import annotations

import json
import os
import socket


class StorageClient:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30.0)
        self.sock.connect(path)
        self._buf = b""

    def _call(self, payload: dict) -> dict:
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("storage-sim closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        resp = json.loads(line)
        if not resp.get("ok", True):
            raise RuntimeError(f"storage-sim error: {resp.get('error')}")
        return resp

    def present(self, blocks: list[bytes]) -> list[bool]:
        resp = self._call({"op": "present", "blocks_hex": [b.hex() for b in blocks]})
        return resp["present"]

    def submit(self, worker: int, rid: str, blocks: list[bytes], demand: float | None) -> None:
        self._call(
            {
                "op": "submit",
                "worker": worker,
                "rid": rid,
                "blocks_hex": [b.hex() for b in blocks],
                "demand": demand,
            }
        )

    def finished(self, worker: int) -> list[str]:
        return self._call({"op": "finished", "worker": worker})["rids"]

    def stats(self) -> dict:
        return self._call({"op": "stats"})

    def close(self) -> None:
        self.sock.close()


def client_from_env() -> "StorageClient | None":
    """按 SIM_STACK_STORAGE_SOCKET 构造客户端；未设置（无远端 KV 模式）返回 None。"""
    path = os.environ.get("SIM_STACK_STORAGE_SOCKET")
    if not path:
        return None
    client = StorageClient(path)
    return client


def default_demand() -> float | None:
    """与 DES run.py 同源的需求推导：一层传输字节 / 一层计算时间。

    仅 max_min_fair 策略消费；进程栈不支持 trace 行级 demand_Bps 覆盖
    （HTTP 请求不携带该字段），文档已声明。
    """
    import json as _json
    from pathlib import Path

    from sim.stack.executor import ROOT

    path = os.environ.get("SIM_STACK_CONFIG", str(ROOT / "configs/demo.json"))
    cfg = _json.loads(Path(path).read_text())
    return cfg["storage"]["kv_bytes_per_token"] / cfg["compute"]["prefill_token_s"]
