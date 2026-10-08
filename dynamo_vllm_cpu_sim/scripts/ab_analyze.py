"""A/B 双类带宽争抢实验的结果分析（docs/AB双类带宽争抢实验设计-20261008.md §5）。

用法：
  .venv/bin/python scripts/ab_analyze.py results/ab_block_b1.0 [results/ab_interleave_b1.0 ...]

按 id 末字符分 A/B 类输出：每类 TTFT mean/p50/p95/max、按类 SLO 达标率
（默认 α=4 × 单条理想 TTFT：A 239.4ms、B 925.1ms，对齐 E26 的
deadline=arrival+α×T0 口径）、每类读字节；以及系统级"并发远端等待"时间线
（各 worker 最近一次 step_complete 的 waiting_remote 快照求并集）的
峰值/p90/时间加权均值——E26d concA 口径的等价量。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def cls_of(rid: str) -> str:
    return rid.rstrip("0123456789s")[-1].upper()


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def analyze(directory: Path, kv_bytes: float, slo: dict):
    r = json.loads((directory / "report.json").read_text())
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
    ]

    per = {c: {"ttft": [], "read_tokens": 0} for c in "AB"}
    for rid, s in r["requests"].items():
        c = cls_of(rid)
        per[c]["ttft"].append(s["ttft_s"])
        per[c]["read_tokens"] += s.get("external_tokens", 0)

    # 系统级并发远端等待：增量维护各 worker 快照的并集
    latest = {}
    ref = Counter()
    series = []  # (t, n_total, n_A)
    for e in events:
        if e["event"] != "step_complete":
            continue
        w = e["worker"]
        for rid in latest.get(w, ()):
            ref[rid] -= 1
            if not ref[rid]:
                del ref[rid]
        latest[w] = list(e["waiting_remote"])
        for rid in latest[w]:
            ref[rid] += 1
        series.append(
            (e["t"], len(ref), sum(1 for rid in ref if cls_of(rid) == "A"))
        )
    conc = {"total": [s[1] for s in series], "A": [s[2] for s in series]}
    spans = [
        (series[i + 1][0] - series[i][0], series[i][1], series[i][2])
        for i in range(len(series) - 1)
    ]
    total_t = sum(d for d, _, _ in spans) or 1.0
    tw = {
        "total": sum(dt * n for dt, n, _ in spans) / total_t,
        "A": sum(dt * na for dt, _, na in spans) / total_t,
    }

    out = {
        "name": directory.name,
        "makespan_s": round(r["makespan_s"], 4),
        "bytes_read_GB": round(r["bytes_read"] / 1e9, 4),
        "compute_s": round(sum(w["compute_s"] for w in r["workers"]), 3),
        "stall_s": round(sum(w["stall_s"] for w in r["workers"]), 3),
        "idle_s": round(sum(w["idle_s"] for w in r["workers"]), 3),
        "classes": {},
        "concurrent_remote_wait": {
            k: {
                "peak": max(v) if v else 0,
                "p90": quantile(sorted(v), 0.9),
                "tw_mean": round(tw[k], 2),
            }
            for k, v in conc.items()
        },
    }
    for c, d in per.items():
        tt = sorted(d["ttft"])
        out["classes"][c] = {
            "n": len(tt),
            "read_GB": round(d["read_tokens"] * kv_bytes / 1e9, 4),
            "ttft_mean_s": round(sum(tt) / len(tt), 4) if tt else None,
            "ttft_p50_s": round(quantile(tt, 0.5), 4),
            "ttft_p95_s": round(quantile(tt, 0.95), 4),
            "ttft_max_s": round(tt[-1], 4) if tt else None,
            "slo_rate": round(sum(t <= slo[c] for t in tt) / len(tt), 4)
            if tt else None,
            "slo_s": slo[c],
        }
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dirs", nargs="+", type=Path)
    p.add_argument("--config", type=Path, default=Path("configs/ab.json"))
    p.add_argument("--alpha", type=float, default=4.0,
                   help="按类 SLO = α × 单条理想 TTFT（默认 4）")
    p.add_argument("--slo-a", type=float, default=None)
    p.add_argument("--slo-b", type=float, default=None)
    a = p.parse_args()
    c = json.loads(a.config.read_text())
    kv = c["storage"]["kv_bytes_per_token"]
    # 单条理想 TTFT = 全量读（无争抢满带宽）+ prefill 计算（虚拟 token 标定值）
    ideal = {
        "A": 130816 * kv / c["storage"]["shared_link_Bps"] + 862 * c["compute"]["prefill_token_s"],
        "B": 28672 * kv / c["storage"]["shared_link_Bps"] + 4096 * c["compute"]["prefill_token_s"],
    }
    slo = {"A": a.slo_a or round(a.alpha * ideal["A"], 4),
           "B": a.slo_b or round(a.alpha * ideal["B"], 4)}
    print(f"按类 SLO（α={a.alpha:g}）: A={slo['A']}s  B={slo['B']}s  "
          f"(单条理想: A={ideal['A']*1e3:.1f}ms B={ideal['B']*1e3:.1f}ms)\n")
    for d in a.dirs:
        print(json.dumps(analyze(d, kv, slo), ensure_ascii=False, indent=2))
        print()


if __name__ == "__main__":
    main()
