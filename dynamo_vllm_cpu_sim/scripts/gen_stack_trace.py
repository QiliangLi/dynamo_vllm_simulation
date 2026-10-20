"""进程栈远端 KV 冒烟 trace 生成器（W5）。

token 全部取自真实文本的编码（GPT-2 BPE），保证 replay_http.py 的
decode→encode 往返校验通过；多行共享长前缀，remote_prefix_tokens 按
block_size 对齐，供 storage-sim 预置共享池（与 DES 的 trace 语义一致：
声明的远端前缀在服务开始前已存在于共享存储）。

用法::

    .venv/bin/python scripts/gen_stack_trace.py --output configs/stack_remote.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BLOCK = 16


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="configs/stack_remote.jsonl")
    ap.add_argument("--block-size", type=int, default=BLOCK)
    ap.add_argument("--n", type=int, default=8, help="请求数")
    a = ap.parse_args()

    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(str(ROOT / "configs/model"))
    shared = tok.encode(
        "shared system context for the distributed inference experiment "
        "with real dynamo routing and real vllm scheduling across worker processes. "
        "the router tracks in flight requests while every engine builds batches, "
        "the storage simulator queues remote reads and the mock executor sleeps, "
        "so the scheduling decisions and bandwidth contention are the real code paths "
    )
    tails = [
        "first request asks about scheduling",
        "second request asks about bandwidth",
        "third request asks about kv blocks",
        "fourth request asks about routing",
        "fifth request asks about prefill",
        "sixth request asks about decode",
        "seventh request asks about queues",
        "eighth request asks about latency",
    ]
    block = a.block_size
    remote_blocks = (len(shared) // block - 1) * block  # 留最后一个块本地重算
    rows = []
    for i in range(a.n):
        ids = shared + tok.encode(f" {tails[i % len(tails)]} number {i}")
        rows.append(
            {
                "id": f"k{i:03d}",
                "arrival_s": round(i * 0.01, 3),
                "prompt_token_ids": ids,
                "output_tokens": 8,
                "remote_prefix_tokens": remote_blocks,
                "priority": 0,
            }
        )
    out = Path(a.output)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # 自检：decode→encode 往返必须无损
    for r in rows:
        assert tok.encode(tok.decode(r["prompt_token_ids"])) == r["prompt_token_ids"], r["id"]
        assert r["remote_prefix_tokens"] % block == 0
    print(f"wrote {out} rows={len(rows)} prompt_len={len(rows[0]['prompt_token_ids'])} remote={remote_blocks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
