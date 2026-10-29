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
        # Check each received fragment once. Scanning and copying the whole
        # prefix on each recv made full block-history exports quadratic.
        pieces = [self._buf]
        chunk = self._buf
        while b"\n" not in chunk:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("storage-sim closed")
            pieces.append(chunk)
        line, self._buf = b"".join(pieces).split(b"\n", 1)
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

    def bind(self, engine_id, source_id, prompt_token_ids, hit_tokens):
        self._call(dict(op="bind",engine_id=engine_id,source_id=source_id,
                        prompt_token_ids=prompt_token_ids,hit_tokens=hit_tokens))

    def present_for_request(self, rid, start_block, blocks):
        return self._call(dict(op="present_for_request",rid=rid,start_block=start_block,
                              blocks_hex=[b.hex() for b in blocks]))["present"]

    def finished(self, worker: int) -> list[str]:
        return self._call({"op": "finished", "worker": worker})["rids"]

    def stats(self) -> dict:
        return self._call({"op": "stats"})

    def gc_begin(self):
        return self._call(dict(op='gc_control', action='begin'))['gc_window']

    def gc_end(self, collect=True):
        return self._call(dict(op='gc_control', action='end', collect=collect))['gc_window']

    def gc_status(self):
        return self._call(dict(op='gc_control', action='status'))['gc_window']

    def layer_register(self, worker, rid, blocks, demand):
        return self._call(dict(op="layer_register",worker=worker,rid=rid,
                               blocks_hex=[b.hex() for b in blocks],demand=demand))

    def layer_submit(self, worker, rids, layer, **options):
        return self._call(dict(op="layer_submit",worker=worker,rids=rids,layer=layer,**options))

    def layer_status(self, worker, rids, layer):
        return self._call(dict(op="layer_status",worker=worker,rids=rids,layer=layer))

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
    if cfg["compute"].get("mode") == "sa_e26f":
        return cfg["storage"]["q_max_Bps"]  # Registration only; allocation uses the real batch's V/c.
    if cfg["compute"].get("mode") == "glm5_d950pr":
        return None  # 接收请求后按其公开形状计算需求，不能再用线性系数。
    return cfg["storage"]["kv_bytes_per_token"] / cfg["compute"]["prefill_token_s"]
