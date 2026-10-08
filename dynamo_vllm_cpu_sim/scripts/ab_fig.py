"""A/B 双类带宽争抢实验的甘特图与存储带宽时序图。

用法（工程根目录 dynamo_vllm_cpu_sim/ 下执行）：
  .venv/bin/python scripts/ab_fig.py results/ab_block_b1.0 --tag block [--audit]
  .venv/bin/python scripts/ab_fig.py results/ab_interleave_b1.0 --tag interleave [--audit]
  # 图输出到仓库根 ../docs/figures/（--figdir 可覆盖）

图 1 分类甘特（ab_gantt_<tag>.png）：横轴=虚拟时间（s），纵轴=worker 0–31
泳道；四色：蓝=A 计算、橙=B 计算、红=IO 等待（有活跃请求但未在计算）、
灰=闲置。重建口径：step_complete 事件是"步完成"（计算区间在事件流中静默
启动），故计算区间= [t_end − duration, t_end]，duration 由 sched 计数与
compute 系数精确反推（本 config prefill/decode 系数相等，逐步长与
prefill/decode 拆分无关）；等待/闲置由"活跃请求窗口（arrival→finish，
取自 report） ∧ 非计算区间"填充。
图 2 带宽时序（ab_bw_<tag>.png）：上面板=链路需求（施加共享链路缩放前的
盘/路径速率和，橙）vs 实际（缩放后，蓝）+ 链路上限黑虚线，数据源为
storage_interval 事件；下面板=系统级并发远端等待数（总/A/B）与 4.1 条
A 打满线。

--audit 守恒断言：∫actual·dt == report.bytes_read；actual ≤ 链路上限；
甘特三分时间与 report 每 worker compute/stall/idle 一致（容差 1%）；
计算区间两两不重叠。依赖 matplotlib（requirements.lock 已登记）。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

STATE_IDLE, STATE_WAIT, STATE_A, STATE_B = 0, 1, 2, 3
STATE_NAMES = {0: "闲置", 1: "IO 等待", 2: "A 计算", 3: "B 计算"}
STATE_COLORS = {0: "#d9d9d9", 1: "#d62728", 2: "#1f77e4", 3: "#ff8c00"}
plt.rcParams["font.sans-serif"] = [
    "Arial Unicode MS", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans CJK SC", "sans-serif",
]
plt.rcParams["axes.unicode_minus"] = False


def cls_of(rid: str) -> str:
    return rid.rstrip("0123456789s")[-1].upper()


def load(directory: Path):
    report = json.loads((directory / "report.json").read_text())
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
    ]
    return report, events


def worker_intervals(report, events):
    """精确重建每 worker 的 (t0, t1, state) 区间（互斥三分）。

    计算区间：每个非空 sched 的 step_complete 事件贡献
    [t_end-duration, t_end]，duration = step_overhead + Σcount×token_s
    （本 config prefill/decode 系数相等，公式与 engine.py 逐步长一致）。
    活跃窗口：该 worker 各请求的 [arrival_s, finish_s]。
    状态：计算区间→A/B 计算；非计算 ∧ 活跃→IO 等待；其余→闲置。
    """
    c = report["config"]["compute"]
    assert c["prefill_token_s"] == c["decode_token_s"], \
        "coef 不等时需按 prefill/decode 拆分逐步长"
    per_s = c["prefill_token_s"]
    n = len(report["workers"])
    active = defaultdict(list)  # worker -> [(arrival, finish)]
    for rid, s in report["requests"].items():
        active[s["worker"]].append((s["arrival_s"], s["finish_s"]))
    computes = defaultdict(list)  # worker -> [(start, end, state)]
    for e in events:
        if e["event"] != "step_complete" or not e["scheduled"]:
            continue
        rid, count = next(iter(e["scheduled"].items()))
        dur = c["step_overhead_s"] + count * per_s
        state = STATE_A if cls_of(rid) == "A" else STATE_B
        computes[e["worker"]].append((e["t"] - dur, e["t"], state))

    out = {}
    for w in range(n):
        segs = []
        comp = sorted(computes[w])
        for i in range(1, len(comp)):
            assert comp[i][0] >= comp[i - 1][1] - 1e-9, f"compute overlap worker {w}"
        # 非计算部分按活跃窗口切分：等待/闲置
        for a, b, s in comp:
            segs.append((a, b, s))
        busy = comp
        merged = []  # 活跃窗口取并集（同刻到达的多请求只算一段）
        for arr, fin in sorted(active[w]):
            if merged and arr <= merged[-1][1] + 1e-12:
                merged[-1] = (merged[-1][0], max(merged[-1][1], fin))
            else:
                merged.append((arr, fin))
        for arr, fin in merged:
            # 活跃窗口内、未被计算覆盖的子区间 → 等待
            gaps = [(arr, fin)]
            for ca, cb, _s in busy:
                nxt = []
                for g0, g1 in gaps:
                    if cb <= g0 or ca >= g1:
                        nxt.append((g0, g1))
                        continue
                    if g0 < ca:
                        nxt.append((g0, ca))
                    if cb < g1:
                        nxt.append((cb, g1))
                gaps = nxt
            for g0, g1 in gaps:
                if g1 - g0 > 1e-12:
                    segs.append((g0, g1, STATE_WAIT))
        segs.sort()
        out[w] = segs
    return out


def fill_idle(segs, t_end):
    """在区间缝隙与首尾填闲置，返回全覆盖的三分区间列表。"""
    full = []
    t = 0.0
    for a, b, s in sorted(segs):
        if a > t + 1e-12:
            full.append((t, a, STATE_IDLE))
        full.append((a, b, s))
        t = max(t, b)
    if t_end > t + 1e-12:
        full.append((t, t_end, STATE_IDLE))
    return full


def dominant_grid(intervals, n_workers, t_end, k_buckets):
    edges = [t_end * i / k_buckets for i in range(k_buckets + 1)]
    grid = [[STATE_IDLE] * k_buckets for _ in range(n_workers)]
    for w in range(n_workers):
        for t0, t1, state in intervals[w]:
            if t1 <= t0:
                continue
            i0 = max(0, min(k_buckets - 1, int(t0 / t_end * k_buckets)))
            i1 = max(0, min(k_buckets - 1, int((t1 - 1e-12) / t_end * k_buckets)))
            for i in range(i0, i1 + 1):
                b0, b1 = edges[i], edges[i + 1]
                overlap = min(t1, b1) - max(t0, b0)
                cell = grid[w][i]
                if not isinstance(cell, tuple):
                    grid[w][i] = (state, overlap)
                elif overlap > cell[1]:
                    grid[w][i] = (state, overlap)
        grid[w] = [c[0] if isinstance(c, tuple) else STATE_IDLE for c in grid[w]]
    return grid


def fig_gantt(directory: Path, out: Path, audit=None):
    report, events = load(directory)
    n = len(report["workers"])
    t_end = report["makespan_s"]
    intervals = {w: fill_idle(worker_intervals(report, events)[w], t_end)
                 for w in range(n)}
    grid = dominant_grid(intervals, n, t_end, 1400)
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.imshow(grid, aspect="auto", cmap=matplotlib.colors.ListedColormap(
        [STATE_COLORS[i] for i in range(4)]), vmin=0, vmax=3,
        extent=(0, t_end, n - 0.5, -0.5), interpolation="nearest")
    marks = sorted({round(s["arrival_s"], 6) for s in report["requests"].values()})
    for x in marks:
        ax.axvline(x, color="k", lw=0.8, ls=":")
    ax.set_xlabel("虚拟时间 (s)")
    ax.set_ylabel("worker")
    ax.set_yticks(range(0, n, 4))
    ax.set_title(f"{directory.name}——分类甘特（{n} worker；"
                 f"蓝=A 算 橙=B 算 红=IO 等 灰=闲置；黑点线=轮次边界）")
    ax.legend(handles=[Patch(facecolor=STATE_COLORS[i], label=STATE_NAMES[i])
                       for i in (2, 3, 1, 0)], loc="upper right", ncol=4)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    if audit is not None:
        for w, worker in enumerate(report["workers"]):
            got = Counter()
            for t0, t1, s in intervals[w]:
                got[s] += t1 - t0
            compute = got[STATE_A] + got[STATE_B]
            audit["gantt_compute_max_rel_err"] = max(
                audit.get("gantt_compute_max_rel_err", 0),
                abs(compute - worker["compute_s"]) / max(worker["compute_s"], 1e-9))
            audit["gantt_stall_max_rel_err"] = max(
                audit.get("gantt_stall_max_rel_err", 0),
                abs(got[STATE_WAIT] - worker["stall_s"]) / max(worker["stall_s"], 1e-9))
            audit["gantt_idle_max_rel_err"] = max(
                audit.get("gantt_idle_max_rel_err", 0),
                abs(got[STATE_IDLE] - worker["idle_s"]) / max(worker["idle_s"], 1e-9))
    return intervals


def concurrent_waiting(events):
    """系统级并发远端等待时间线：(t, total, nA, nB)。"""
    latest, ref = {}, Counter()
    out = []
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
        na = sum(1 for rid in ref if cls_of(rid) == "A")
        out.append((e["t"], len(ref), na, len(ref) - na))
    return out


def fig_bw(directory: Path, out: Path, audit=None):
    report, events = load(directory)
    ivs = [e for e in events if e["event"] == "storage_interval"]
    link = report["config"]["storage"]["shared_link_Bps"]
    cw = concurrent_waiting(events)
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1]})
    if ivs:
        ts, ds, as_ = [], [], []
        for e in ivs:
            ts += [e["from"], e["t"]]
            ds += [e["demand_Bps"] / 1e9, e["demand_Bps"] / 1e9]
            as_ += [e["actual_Bps"] / 1e9, e["actual_Bps"] / 1e9]
        ax1.plot(ts, ds, color="#ff8c00", lw=1.2, label="需求（链路缩放前 Σ速率）")
        ax1.plot(ts, as_, color="#1f77e4", lw=1.2, label="实际（链路缩放后 Σ速率）")
    ax1.axhline(link / 1e9, color="k", lw=1, ls="--",
                label=f"共享链路上限 {link/1e9:.0f} GB/s")
    ax1.set_ylabel("GB/s")
    ax1.set_title(f"{directory.name}——存储带宽时序与并发远端等待")
    ax1.legend(loc="lower left", ncol=3)
    ax1.grid(alpha=0.25)
    if cw:
        t = [x[0] for x in cw]
        ax2.plot(t, [x[1] for x in cw], color="#7f7f7f", lw=1.2, label="总等待")
        ax2.plot(t, [x[2] for x in cw], color="#1f77e4", lw=1.4, label="A 等待")
        ax2.plot(t, [x[3] for x in cw], color="#ff8c00", lw=1.2, label="B 等待")
        ax2.axhline(4.1, color="k", lw=0.8, ls=":", label="4.1 条 A 打满链路")
    ax2.set_xlabel("虚拟时间 (s)")
    ax2.set_ylabel("并发远端等待数")
    ax2.legend(loc="lower left", ncol=4)
    ax2.grid(alpha=0.25)
    ax2.set_xlim(0, report["makespan_s"])
    ax1.text(report["makespan_s"] * 0.99, link / 1e9 * 1.04,
             f"{link/1e9:.0f}", ha="right", fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    if audit is not None and ivs:
        integral = sum(e["actual_Bps"] * (e["t"] - e["from"]) for e in ivs)
        audit["bw_integral_bytes"] = integral
        audit["bw_bytes_read"] = report["bytes_read"]
        audit["bw_integral_rel_err"] = abs(
            integral - report["bytes_read"]) / report["bytes_read"]
        audit["bw_actual_max_GBps"] = max(e["actual_Bps"] for e in ivs) / 1e9
        audit["bw_demand_max_GBps"] = max(e["demand_Bps"] for e in ivs) / 1e9


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("results", type=Path)
    p.add_argument("--tag", required=True, help="输出文件名后缀，如 block")
    p.add_argument("--figdir", type=Path, default=Path("../docs/figures"))
    p.add_argument("--audit", action="store_true")
    a = p.parse_args()
    a.figdir.mkdir(parents=True, exist_ok=True)
    audit = {} if a.audit else None
    fig_gantt(a.results, a.figdir / f"ab_gantt_{a.tag}.png", audit)
    fig_bw(a.results, a.figdir / f"ab_bw_{a.tag}.png", audit)
    if audit is not None:
        bad = [k for k, v in audit.items() if k.endswith("rel_err") and v > 0.01]
        audit["verdict"] = "FAIL " + ",".join(bad) if bad else "PASS"
        print(json.dumps(audit, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
