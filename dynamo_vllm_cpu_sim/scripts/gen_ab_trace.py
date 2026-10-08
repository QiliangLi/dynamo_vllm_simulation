"""A/B 双类带宽争抢 trace 生成器（docs/AB双类带宽争抢实验设计-20261008.md §4）。

工况映射自 status_aware_network 的 E26 大纲：A=输入128K/重算256（每层读
175.65MB/算6.02ms）、B=输入32K/重算4096（38.5MB/28.59ms）。本仓库计算模型
为单一全局线性系数，采用大纲 §3 的"虚拟 token + 重标定"：prefill_token_s
按 B 精确命中（0.22872s/4096），A 的重算虚拟化为 862 token 命中 48.16ms；
读字节经 kv_bytes_per_token=10741.744 由字面前缀长度精确命中（A 1.4052GB、
B 0.30798GB）。语义说明见设计文档 §2，结果文档不得隐瞒。

顺序由 id 字典序决定（同轮同刻到达）：block=32A 在前+64B；interleave=
(A,B,B)×32。每请求 token 唯一（(ordinal*7919+j)%32000，7919 与 32000
互素 → 首 token 即互异），避免本地前缀缓存命中吞掉远端读。

输出到 results/traces/（gitignored，约 70–150MB，只入库生成器不入库产物）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# 类参数：(remote_prefix_tokens, recompute_tokens, output_tokens)
A_CLASS = (130816, 862, 16)
B_CLASS = (28672, 4096, 16)
VOCAB = 32000
TOKEN_STRIDE = 7919  # 与 VOCAB 互素，保证不同请求的 token 序列从首 token 起互异


def _row(ordinal: int, rid: str, arrival_s: float, cls: str) -> dict:
    remote, recompute, output = A_CLASS if cls == "A" else B_CLASS
    prompt = remote + recompute
    tokens = [(ordinal * TOKEN_STRIDE + j) % VOCAB for j in range(prompt)]
    return {
        "id": rid,
        "arrival_s": arrival_s,
        "prompt_token_ids": tokens,
        "output_tokens": output,
        "remote_prefix_tokens": remote,
    }


def gen_rows(order: str, rounds: int = 2, burst_s: float = 1.0,
             n_a: int = 32, n_b: int = 64) -> list[dict]:
    """生成 trace 行。同轮所有请求 arrival_s 相同，顺序完全由 id 字典序决定。"""
    if order not in ("block", "interleave"):
        raise ValueError(f"order must be block|interleave, got {order}")
    if n_b != 2 * n_a:
        raise ValueError("A:B must be 1:2 (n_b == 2*n_a)")
    rows = []
    for r in range(rounds):
        t = round(r * burst_s, 6)
        if order == "block":
            plan = [("A", f"r{r}a{i:03d}") for i in range(n_a)] + [
                ("B", f"r{r}b{i:03d}") for i in range(n_b)
            ]
        else:  # interleave: (A,B,B) × n_a
            plan = []
            for g in range(n_a):
                plan.append(("A", f"r{r}s{3 * g:03d}A"))
                plan.extend(("B", f"r{r}s{3 * g + k:03d}B") for k in (1, 2))
        for ordinal, (cls, rid) in enumerate(plan):
            rows.append(_row(r * 96 + ordinal, rid, t, cls))
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--order", choices=["block", "interleave"], default="block")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--burst-s", type=float, default=1.0)
    p.add_argument("--n-a", type=int, default=32)
    p.add_argument("--n-b", type=int, default=64)
    p.add_argument("--smoke", action="store_true",
                   help="冒烟规模：8A+16B × 1 轮")
    p.add_argument("--output", type=Path, default=None)
    a = p.parse_args()
    n_a, n_b, rounds = (8, 16, 1) if a.smoke else (a.n_a, a.n_b, a.rounds)
    rows = gen_rows(a.order, rounds, a.burst_s, n_a, n_b)
    out = a.output or Path("results/traces") / (
        f"ab_{a.order}_r{rounds}_b{a.burst_s:g}"
        f"{'_smoke' if a.smoke else ''}.jsonl"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    gb = 10741.744 / 1e9

    def cls_of(row):
        return row["id"].rstrip("0123456789s")[-1:].upper()

    reads = {c: sum(r["remote_prefix_tokens"] for r in rows if cls_of(r) == c)
             for c in "AB"}
    print(json.dumps({
        "output": str(out), "order": a.order, "rows": len(rows),
        "n_A": sum(cls_of(r) == "A" for r in rows),
        "n_B": sum(cls_of(r) == "B" for r in rows),
        "read_GB": {"A": round(reads["A"] * gb, 4), "B": round(reads["B"] * gb, 4)},
        "drain_lower_bound_s": round((reads["A"] + reads["B"]) * gb / 120, 4),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
