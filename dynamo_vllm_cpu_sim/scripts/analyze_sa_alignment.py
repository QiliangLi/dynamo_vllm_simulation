#!/usr/bin/env python3
"""完整验收后汇总 SA 对齐矩阵；固定选 seed 416/424、rep0 作轨迹图。"""

import argparse
import csv
from collections import defaultdict
import hashlib
import gzip
import json
import math
from pathlib import Path
import shutil
import statistics


def median(rows, key):
    return statistics.median(r[key] for r in rows)


def write_csv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def analyze(root, archive, figures):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "savefig.dpi": 160,
        }
    )
    manifest = json.loads((root / "manifest.json").read_text())
    source = json.loads((root / "source/source.json").read_text())
    records, requests, classes, hashes = [], [], [], []
    reports = {}
    for job in manifest:
        folder = Path(job["output"])
        report = json.loads((folder / "report.json").read_text())
        audit = (folder / "check.log").read_text()
        assert "PASS" in audit and report["main_experiment_valid"]
        assert report["implementation_sha256"] == job["implementation_sha256"]
        assert report["config"] == job["config"]
        assert report["config"]["source_sha256"] == source["source_sha256"]
        trace = json.loads(Path(job["trace"]).read_text())
        assert (
            report["workload_sha256"]
            == hashlib.sha256(
                json.dumps(
                    sorted(trace, key=lambda r: (r["arrival_s"], r["id"])),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        identity = {k: job[k] for k in ("variant", "seed", "cap", "mode", "repeat")}
        record = {
            **identity,
            "slo": report["profile_deadline_attainment"],
            "ttft_ms": report["mean_ttft_s"] * 1000,
            "central_wait_ms": statistics.mean(
                s["central_wait_s"] for s in report["requests"].values()
            )
            * 1000,
            "makespan_s": report["makespan_s"],
            "batch_mean": report["actual_batch"]["requests"]["mean"],
            "batch_max": max(
                map(int, report["actual_batch"]["request_count_histogram"])
            ),
            "compute_s": sum(w["compute_s"] for w in report["workers"]),
            "stall_s": sum(w["stall_s"] for w in report["workers"]),
            "bytes_read": report["bytes_read"],
            "peak_memory_gb": max(w["peak_memory_bytes"] for w in report["workers"])
            / 1e9,
        }
        step_events = [
            json.loads(line)
            for line in (folder / "events.jsonl").read_text().splitlines()
            if '"step_started"' in line
        ]
        mixed = [
            e
            for e in step_events
            if len(
                {
                    report["requests"][rid]["class_id"]
                    for rid in e["num_scheduled_tokens"]
                }
            )
            > 1
        ]
        record["mixed_steps"] = len(mixed)
        record["C_members_in_read_steps"] = sum(
            sum(
                report["requests"][rid]["class_id"] == "C"
                for rid in e["num_scheduled_tokens"]
            )
            for e in step_events
            if e["layer_read_bytes"] > 0
        )
        records.append(record)
        for cls, s in report["classes"].items():
            classes.append(
                {
                    **identity,
                    "class": cls,
                    "slo": s["profile_deadline_attainment"],
                    "ttft_ms": s["ttft_s"]["mean"] * 1000,
                    "ttft_p95_ms": s["ttft_s"]["p95"] * 1000,
                    "central_wait_ms": s["central_wait_s"]["mean"] * 1000,
                }
            )
        for rid, s in report["requests"].items():
            requests.append(
                {
                    **identity,
                    "id": rid,
                    **{
                        k: s[k]
                        for k in (
                            "class_id",
                            "h_tokens",
                            "u_tokens",
                            "arrival_s",
                            "deadline_s",
                            "worker",
                            "submitted_s",
                            "load_submit_s",
                            "load_done_s",
                            "first_token_s",
                            "ttft_s",
                            "central_wait_s",
                            "submitted_read_bytes",
                            "external_tokens",
                            "preemptions",
                        )
                    },
                }
            )
        for name in ("report.json", "events.jsonl", "config.json", "check.log"):
            path = folder / name
            hashes.append(
                {
                    "cell": str(folder.relative_to(root)),
                    "file": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        reports[
            (job["variant"], job["seed"], job["cap"], job["mode"], job["repeat"])
        ] = (job, report)
    assert len({j["implementation_sha256"] for j in manifest}) == 1
    assert len(records) == 96, (
        "do not publish a partial matrix as the formal experiment"
    )
    per_seed = []
    groups = defaultdict(list)
    metrics = [
        k for k in records[0] if k not in ("variant", "seed", "cap", "mode", "repeat")
    ]
    for record in records:
        groups[tuple(record[k] for k in ("variant", "seed", "cap", "mode"))].append(
            record
        )
    for key, rs in sorted(groups.items()):
        assert len(rs) == 3
        per_seed.append(
            {
                **dict(zip(("variant", "seed", "cap", "mode"), key)),
                **{k: median(rs, k) for k in metrics},
                "slo_min": min(r["slo"] for r in rs),
                "slo_max": max(r["slo"] for r in rs),
            }
        )
    groups = defaultdict(list)
    for row in per_seed:
        groups[(row["variant"], row["cap"], row["mode"])].append(row)
    summary = [
        {
            "variant": v,
            "cap": cap,
            "mode": mode,
            "seed_count": len(rs),
            **{k: median(rs, k) for k in metrics},
        }
        for (v, cap, mode), rs in sorted(groups.items())
    ]
    sa = []
    for cell in source["cells"]:
        for mode, filename in cell["controls"].items():
            r = json.loads((root / "source" / filename).read_text())
            sa.append(
                {
                    "variant": cell["variant"],
                    "seed": cell["seed"],
                    "mode": mode,
                    "slo": statistics.mean(
                        s["F_s"] <= s["deadline_s"] for s in r["requests"]
                    ),
                    "ttft_ms": statistics.mean(
                        s["F_s"] - s["arrival_s"] for s in r["requests"]
                    )
                    * 1000,
                    "makespan_s": max(s["F_s"] for s in r["requests"]),
                    "bytes_read": r["storage_bytes"],
                }
            )
    archive.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    for name, rows in (
        ("runs", records),
        ("per_seed", per_seed),
        ("summary", summary),
        ("classes", classes),
        ("requests", requests),
        ("sa_controls", sa),
        ("raw_hashes", hashes),
    ):
        write_csv(archive / f"{name}.csv", rows)
    for name in ("source", "verification"):
        shutil.copytree(root / name, archive / name, dirs_exist_ok=True)
    shutil.copy2(root / "verification.json", archive / "verification.json")
    # 自包含归档无需原始绝对路径才可阅读；运行路径另保留在 manifest 以便核对。
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (archive / "summary.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "sa_controls": sa,
                "implementation_sha256": manifest[0]["implementation_sha256"],
                "runs": len(records),
                "aggregation": "median of 3 routing repeats per seed, then median across official seeds",
                "source_commit": source["commit"],
            },
            indent=2,
        )
        + "\n"
    )
    # 四个固定示例保存完整原始事件（压缩但不改内容），便于离开本机后复核时序图。
    for variant, seed in (("A", 416), ("B", 424)):
        plot_end = max(
            reports[(variant, seed, 8, mode, 0)][1]["makespan_s"]
            for mode in ("immediate", "tfs")
        )
        for mode in ("immediate", "tfs"):
            job, _ = reports[(variant, seed, 8, mode, 0)]
            original = Path(job["output"])
            selected = archive / "selected_runs" / f"C{variant}-s{seed}-cap8-{mode}-r0"
            selected.mkdir(parents=True, exist_ok=True)
            for name in ("report.json", "config.json", "check.log"):
                shutil.copy2(original / name, selected / name)
            data = (original / "events.jsonl").read_bytes()
            compressed = gzip.compress(data, mtime=0)
            assert gzip.decompress(compressed) == data
            (selected / "events.jsonl.gz").write_bytes(compressed)
    header = "|C 类型|请求数上限|策略|期限达标率|平均 TTFT ms|中央等待 ms|实际 batch 均值|\n|---|---:|---|---:|---:|---:|---:|\n"
    lines = [
        f"|C_{r['variant']}|{r['cap']}|{r['mode']}|{100 * r['slo']:.1f}%|{r['ttft_ms']:.2f}|{r['central_wait_ms']:.2f}|{r['batch_mean']:.2f}|"
        for r in summary
    ]
    (archive / "summary.md").write_text(header + "\n".join(lines) + "\n")
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=True, layout="constrained")
    colors = {
        "immediate": "#597c9b",
        "slack_only": "#92a9bc",
        "quota_fcfs": "#8b9d63",
        "tfs": "#b35a3a",
    }
    for ax, variant in zip(axes, "AB"):
        selected = [r for r in summary if r["variant"] == variant]
        labels, values, cs = [], [], []
        for mode in ("fcfs", "tfs"):
            rs = [r for r in sa if r["variant"] == variant and r["mode"] == mode]
            labels.append("SA\n" + mode)
            values.append(median(rs, "slo") * 100)
            cs.append("#333333" if mode == "fcfs" else "#d0a052")
        for cap in (1, 8):
            for mode in colors:
                row = next(r for r in selected if r["cap"] == cap and r["mode"] == mode)
                labels.append(f"DV {cap}\n{mode}")
                values.append(row["slo"] * 100)
                cs.append(colors[mode])
        bars = ax.bar(range(len(values)), values, color=cs, width=0.72)
        ax.bar_label(bars, fmt="%.1f", fontsize=8, padding=3)
        ax.set_xticks(range(len(labels)), labels, rotation=45, ha="right", fontsize=8)
        ax.set_title(
            f"C_{variant}: {'storage' if variant == 'A' else 'compute'} pressure"
        )
        ax.set_ylim(0, 100)
        ax.set_ylabel("Request deadline attainment (%)")
    fig.savefig(figures / "sa_alignment_attainment.png")
    plt.close(fig)
    for variant, seed in (("A", 416), ("B", 424)):
        fig, axes = plt.subplots(
            4,
            1,
            figsize=(14, 13),
            sharex=True,
            layout="constrained",
            gridspec_kw={"height_ratios": [2, 2, 1, 1]},
        )
        fig.legend(
            handles=[
                Patch(color=color, label=label)
                for label, color in (
                    ("A", "#597c9b"),
                    ("B", "#d39550"),
                    ("C (zero read)", "#6c966f"),
                    ("Mixed batch", "#9974a5"),
                )
            ],
            loc="outside upper center",
            ncol=4,
            frameon=False,
        )
        for ax, mode in zip(axes[:2], ("immediate", "tfs")):
            job, report = reports[(variant, seed, 8, mode, 0)]
            ev = [
                json.loads(x)
                for x in (Path(job["output"]) / "events.jsonl").read_text().splitlines()
            ]
            starts = {
                (e["worker"], e["step_id"]): e
                for e in ev
                if e["event"] == "step_started"
            }
            for e in ev:
                if e["event"] != "layer_compute_started":
                    continue
                classes_in = {
                    report["requests"][rid]["class_id"]
                    for rid in starts[(e["worker"], e["step_id"])][
                        "num_scheduled_tokens"
                    ]
                }
                color = (
                    {"A": "#597c9b", "B": "#d39550", "C": "#6c966f"}.get(
                        next(iter(classes_in))
                    )
                    if len(classes_in) == 1
                    else "#9974a5"
                )
                ax.broken_barh(
                    [(e["t"], e["end_s"] - e["t"])],
                    (e["worker"] - 0.4, 0.8),
                    facecolors=color,
                )
            ax.set_ylabel(f"{mode}\nWorker")
            ax.set_ylim(-1, 32)
            ax.set_yticks([0, 8, 16, 24, 31])
            intervals = sorted(
                (e for e in ev if e["event"] == "admission_interval"),
                key=lambda x: x["from"],
            )
            axes[2].step(
                [e["from"] for e in intervals],
                [e["queued"] for e in intervals],
                where="post",
                label=mode,
                color=colors[mode],
            )
            bw = sorted(
                (e for e in ev if e["event"] == "storage_interval"),
                key=lambda x: x["from"],
            )
            # 按真实区间字节积分生成 10ms 均值，保留间隙，不用事件瞬时尖峰填满图。
            bin_s = 0.01
            bins = [0.0] * math.ceil(plot_end / bin_s)
            for interval in bw:
                first = int(interval["from"] / bin_s)
                last = min(len(bins) - 1, int(interval["t"] / bin_s))
                for bucket in range(first, last + 1):
                    overlap = max(
                        0.0,
                        min(interval["t"], (bucket + 1) * bin_s)
                        - max(interval["from"], bucket * bin_s),
                    )
                    bins[bucket] += interval["actual_Bps"] * overlap
            axes[3].step(
                [i * bin_s for i in range(len(bins))],
                [value / bin_s / 1e9 for value in bins],
                where="post",
                label=mode,
                color=colors[mode],
                alpha=0.8,
            )
        axes[0].set_title(
            f"C_{variant}, seed {seed}, repeat 0, max_num_seqs=8; compute only (blank = stall/idle)"
        )
        axes[2].set_ylabel("Central queue")
        axes[2].legend(loc="upper right")
        axes[3].set_ylabel("Actual GB/s\n(10 ms bins)")
        axes[3].axhline(120, color="#333333", linestyle="--", linewidth=0.8)
        axes[3].set_ylim(0, 125)
        axes[3].set_xlabel("Simulation time (s)")
        fig.savefig(figures / f"sa_alignment_{variant}_timeline.png")
        plt.close(fig)
    # 独立重复的不确定性：不把多个路由重复误当更多输入种子。
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout="constrained")
    for ax, variant in zip(axes, "AB"):
        data = []
        for cap in (1, 8):
            for mode in colors:
                rs = [
                    r
                    for r in records
                    if r["variant"] == variant and r["cap"] == cap and r["mode"] == mode
                ]
                data.append([r["ttft_ms"] for r in rs])
        ax.boxplot(
            data,
            tick_labels=[f"{cap}/{mode}" for cap in (1, 8) for mode in colors],
            showfliers=True,
        )
        ax.tick_params(axis="x", rotation=45, labelsize=8)
        ax.set_title(f"C_{variant}: all routing repeats, not independent traces")
        ax.set_ylabel("Mean TTFT per run (ms)")
    fig.savefig(figures / "sa_alignment_ttft_spread.png")
    plt.close(fig)
    print(header + "\n".join(lines))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--archive", required=True)
    p.add_argument("--figures", required=True)
    a = p.parse_args()
    analyze(
        Path(a.output).resolve(), Path(a.archive).resolve(), Path(a.figures).resolve()
    )


if __name__ == "__main__":
    main()
