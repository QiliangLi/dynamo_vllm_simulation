#!/usr/bin/env python3
"""逐运行、逐种子和配对统计；图像只使用真实事件，原始结果不改写。"""

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def median(xs):
    return statistics.median(xs) if xs else None


def analyze(root, figures=None):
    root = Path(root)
    cells = json.loads((root / "manifest.json").read_text())
    rows, samples = [], {}
    for cell in cells:
        directory = Path(cell["output"])
        path = directory / "report.json"
        if not path.exists():
            raise ValueError(f"missing report: {path}")
        r = json.loads(path.read_text())
        if r["status"] != "completed":
            raise ValueError(f"incomplete run: {path}")
        mode = r["config"]["admission"]["mode"]
        if not r["config"]["admission"]["aging_enabled"] and mode == "tfs":
            mode = "tfs_no_aging"
        row = {"workload": cell["workload"], "seed": cell["seed"], "repeat": cell["repeat"],
               "mode": mode, "max_num_seqs": r["config"]["max_num_seqs"],
               "release_on": r["config"]["admission"]["release_on"],
               "feedback_delay_s": r["config"]["admission"]["feedback_delay_s"],
               "profile_attainment": r["profile_deadline_attainment"],
               "fixed_attainment": r["ttft_slo_attainment"], "mean_ttft_s": r["mean_ttft_s"],
               "makespan_s": r["makespan_s"], "bytes_read": r["bytes_read"],
               "batch_mean": r["actual_batch"]["requests"]["mean"],
               "batch_p99": r["actual_batch"]["requests"]["p99"],
               "batch_time_weighted_mean": r["actual_batch"]["time_weighted_mean"],
               "compute_busy_fraction": r["simulated_compute_busy_fraction"],
               "peak_read_requests": r["read_concurrency"]["peak_requests"],
               "main_experiment_valid": r["main_experiment_valid"],
               "central_wait_mean_s": statistics.mean(s["central_wait_s"] for s in r["requests"].values()),
               "engine_ttft_mean_s": statistics.mean(s["engine_ttft_s"] for s in r["requests"].values()),
               "report": str(path), "workload_sha256": r["workload_sha256"],
               "classes": r["classes"]}
        rows.append(row)
        key = (cell["workload"], cell["seed"], cell["repeat"], r["config"]["max_num_seqs"])
        samples.setdefault(key, []).append((mode, directory, r))
    # 抢占/重复加载使整组对照不满足主实验假设，而不是只剔除某个策略。
    invalid_groups = {(r["workload"], r["seed"], r["max_num_seqs"]) for r in rows if not r["main_experiment_valid"]}
    for r in rows:
        r["comparison_group_valid"] = (r["workload"], r["seed"], r["max_num_seqs"]) not in invalid_groups
    keys = ("workload", "max_num_seqs", "mode", "release_on", "feedback_delay_s")
    metrics = ("profile_attainment", "fixed_attainment", "mean_ttft_s", "makespan_s",
               "batch_mean", "batch_time_weighted_mean", "compute_busy_fraction",
               "peak_read_requests", "central_wait_mean_s", "engine_ttft_mean_s")
    grouped = defaultdict(list)
    for r in rows:
        grouped[tuple(r[k] for k in keys)].append(r)
    summaries, seed_rows, pairs = [], [], []
    for key, values in sorted(grouped.items()):
        seeds = defaultdict(list)
        for value in values:
            seeds[value["seed"]].append(value)
        per_seed = []
        for seed, vs in sorted(seeds.items()):
            sr = {**dict(zip(keys, key)), "seed": seed, "repeats": len(vs),
                  **{metric: median([v[metric] for v in vs]) for metric in metrics}}
            seed_rows.append(sr)
            per_seed.append(sr)
        summaries.append({**dict(zip(keys, key)), "runs": len(values), "seeds": len(seeds),
                          "all_groups_valid": all(v["comparison_group_valid"] for v in values),
                          **{metric: median([v[metric] for v in per_seed]) for metric in metrics},
                          "observed_batch_gt_one": any(v["batch_p99"] > 1 for v in values)})
    baselines = {(r["workload"], r["max_num_seqs"], r["seed"], r["repeat"]): r for r in rows if r["mode"] == "immediate"}
    for row in rows:
        baseline = baselines.get((row["workload"], row["max_num_seqs"], row["seed"], row["repeat"]))
        if baseline is not None and row["mode"] != "immediate":
            if baseline["workload_sha256"] != row["workload_sha256"]:
                raise ValueError("paired policies do not share an identical trace")
            pairs.append({**{k:row[k] for k in (*keys, "seed", "repeat")},
                          **{f"delta_{m}": row[m]-baseline[m] for m in metrics},
                          "comparison_group_valid": row["comparison_group_valid"]})
    paired_groups = defaultdict(list)
    for pair in pairs:
        paired_groups[tuple(pair[k] for k in keys)].append(pair)
    paired_summary = []
    for key, values in sorted(paired_groups.items()):
        per_seed_pairs = defaultdict(list)
        for value in values:
            per_seed_pairs[value["seed"]].append(value)
        paired_summary.append({**dict(zip(keys, key)), "seeds": len(per_seed_pairs),
                               **{f"delta_{m}": median([median([v[f"delta_{m}"] for v in vs])
                                  for vs in per_seed_pairs.values()]) for m in metrics}})
    class_rows = []
    for row in rows:
        for cls, values in row["classes"].items():
            flat = {k: row[k] for k in (*keys, "seed", "repeat")}
            flat["class_id"] = cls
            for name, value in values.items():
                if isinstance(value, dict):
                    flat.update({f"{name}_{k}": v for k, v in value.items()})
                else:
                    flat[name] = value
            class_rows.append(flat)
    sensitivity_pairs = []
    for axis, baseline_value, metrics_name in (("release_on", "first_token", "release"),
                                               ("feedback_delay_s", 0.0, "feedback"),
                                               ("mode", "tfs", "aging")):
        keep = [k for k in (*keys, "seed", "repeat") if k != axis]
        index = {tuple(r[k] for k in keep): r for r in rows if r[axis] == baseline_value}
        for row in rows:
            if row[axis] == baseline_value or (axis == "mode" and row["mode"] != "tfs_no_aging"):
                continue
            base = index.get(tuple(row[k] for k in keep))
            if base is not None:
                sensitivity_pairs.append({"axis": metrics_name, **{k:row[k] for k in (*keys,"seed","repeat")},
                                          **{f"delta_{m}":row[m]-base[m] for m in metrics}})
    result = {"sensitivity_paired_differences": sensitivity_pairs, "paired_summary_vs_immediate": paired_summary, "aggregation": "median across repeats within seed, then median across seeds",
              "native_routing": "real selector; equal-cost ties may vary independently of trace seed",
              "runs": rows, "per_seed": seed_rows, "paired_differences_vs_immediate": pairs,
              "summary": summaries, "invalid_comparison_groups": sorted(invalid_groups)}
    (root / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    for name, data in (("runs", rows), ("per_seed", seed_rows), ("summary", summaries), ("paired", pairs), ("classes", class_rows)):
        if data:
            fields = [k for k in data[0] if k != "classes"]
            with (root / f"{name}.csv").open("w") as file:
                writer = csv.DictWriter(file, fields, extrasaction="ignore", lineterminator="\n")
                writer.writeheader()
                writer.writerows(data)
    md = ["# TFS 矩阵汇总", "", "先取同一种子的重复中位数，再取跨种子中位数。实际 batch 来自非空 SchedulerOutput。", "",
          "|负载|seq 上限|策略|释放|反馈 ms|画像达标率|平均 TTFT ms|实际 batch 均值|中央等待 ms|",
          "|---|---:|---|---|---:|---:|---:|---:|---:|"]
    for s in summaries:
        md.append(f"|{s['workload']}|{s['max_num_seqs']}|{s['mode']}|{s['release_on']}|{s['feedback_delay_s']*1000:g}|{s['profile_attainment']:.3f}|{s['mean_ttft_s']*1000:.3f}|{s['batch_mean']:.2f}|{s['central_wait_mean_s']*1000:.3f}|")
    (root / "summary.md").write_text("\n".join(md) + "\n")
    if figures is not None:
        render_figures(samples, summaries, Path(figures))
    return result


def render_figures(samples, summaries, destination):
    """每个工况用相同坐标比较甘特、等待分解、batch 和中央积压。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    destination.mkdir(parents=True, exist_ok=True)
    for workload in sorted({k[0] for k in samples}):
        candidates = [k for k in samples if k[0] == workload]
        # 固定选择最小种子、首重复、8 seq（无该档则用最大档），不按性能挑图。
        seed = min(k[1] for k in candidates)
        cap = 8 if any(k[3] == 8 for k in candidates) else max(k[3] for k in candidates)
        key = min((k for k in candidates if k[1] == seed and k[3] == cap), key=lambda k:k[2])
        runs = samples[key]
        if len(runs) > 6:
            # 敏感性大矩阵的图固定展示启用老化、0/5ms；全部 12 组仍完整输出表格。
            runs = [x for x in runs if x[2]["config"]["admission"]["aging_enabled"]
                    and x[2]["config"]["admission"]["feedback_delay_s"] in (0.0, .005)]
        # 敏感性图中用独立标签避免多个同名 tfs 覆盖。
        names = [f"{m}/{r['config']['admission']['release_on']}/{r['config']['admission']['feedback_delay_s']*1000:g}ms" for m,_,r in runs]
        data = [[json.loads(x) for x in (d / "events.jsonl").read_text().splitlines()] for _,d,_ in runs]
        end = max(r["business_end_s"] for _,_,r in runs)
        fig, axs = plt.subplots(len(runs), 1, figsize=(11, max(3, 2.2*len(runs))), squeeze=False)
        for ax, label, es, (_,_,r) in zip(axs[:,0], names, data, runs):
            for e in es:
                if e["event"] == "step_scheduled":
                    ax.broken_barh([(e["t"]*1000,e["duration_s"]*1000)], (e["worker"]-.32,.64), facecolors="#227aa5")
            ax.set(xlim=(0,end*1000), yticks=range(r["config"]["workers"]), ylabel=label)
            ax.grid(axis="x", alpha=.2)
        axs[-1,0].set_xlabel("Virtual time (ms); compute steps only, blank = stall or idle")
        fig.tight_layout();fig.savefig(destination/f"tfs_{workload}_gantt.png",dpi=150);plt.close(fig)
        fig, axs = plt.subplots(2,2,figsize=(12,7))
        for label,es,(_,_,r) in zip(names,data,runs):
            intervals = [e for e in es if e["event"] == "admission_interval"]
            if intervals:
                ts=[e["from"]*1000 for e in intervals]+[intervals[-1]["t"]*1000]
                axs[0,0].step(ts,[e["queued"] for e in intervals]+[intervals[-1]["queued"]],where="post",label=label)
                axs[0,1].step(ts,[e["outstanding_read_requests"] for e in intervals]+[intervals[-1]["outstanding_read_requests"]],where="post",label=label)
        for ax in axs[0]:ax.set_xlim(0,end*1000);ax.grid(alpha=.2);ax.set_xlabel("Virtual time (ms)")
        axs[0,0].set_ylabel("Central queued requests");axs[0,1].set_ylabel("Submitted, unfinished KV reads")
        for label,(_,_,r) in zip(names,runs):
            hist=r["actual_batch"]["request_count_histogram"]
            xs=sorted(map(int,hist));ys=[hist[str(x)] for x in xs]
            axs[1,0].plot(xs,ys,marker="o",label=label)
        axs[1,0].set(xlabel="Actual requests per compute step",ylabel="Number of steps")
        central=[statistics.mean(s["central_wait_s"] for s in r["requests"].values())*1000 for _,_,r in runs]
        engine=[statistics.mean(s["engine_ttft_s"] for s in r["requests"].values())*1000 for _,_,r in runs]
        axs[1,1].bar(range(len(runs)),central,label="Central wait")
        axs[1,1].bar(range(len(runs)),engine,bottom=central,label="Engine-side TTFT")
        axs[1,1].set(xticks=range(len(runs)),xticklabels=names,ylabel="Mean TTFT (ms)")
        axs[1,1].tick_params(axis="x",labelrotation=30)
        axs[0,0].legend(fontsize=7);axs[1,1].legend(fontsize=8)
        fig.tight_layout();fig.savefig(destination/f"tfs_{workload}_queues_batch_wait.png",dpi=150);plt.close(fig)
        fig, axs = plt.subplots(len(runs), 2, figsize=(12, max(3,2.1*len(runs))), squeeze=False)
        for axes, label, es, (_,_,r) in zip(axs, names, data, runs):
            intervals = [e for e in es if e["event"] == "admission_interval"]
            axes[0].step([e["from"]*1000 for e in intervals], [e["heavy_held"] for e in intervals], where="post", label="Held heavy requests")
            axes[0].step([e["from"]*1000 for e in intervals], [e["heavy_cap"] or 0 for e in intervals], where="post", label="Heavy cap")
            axes[0].set_ylabel(label,fontsize=8)
            bw = [e for e in es if e["event"] == "storage_interval"]
            axes[1].step([e["from"]*1000 for e in bw], [e["demand_Bps"]/1e9 for e in bw], where="post", label="Disk/path offered rate")
            axes[1].step([e["from"]*1000 for e in bw], [e["actual_Bps"]/1e9 for e in bw], where="post", label="Actual link rate")
            axes[1].plot([e["from"]*1000 for e in intervals], [e["tfs_pressure_sum_Bps"]/1e9 for e in intervals], linestyle=":", label="TFS estimated pressure sum")
            axes[1].set_yscale("symlog", linthresh=.5)
            axes[1].axhline(r["config"]["storage"]["shared_link_Bps"]/1e9,color="black",linestyle="--",linewidth=.8)
            for ax in axes:ax.set_xlim(0,end*1000);ax.grid(alpha=.2)
        axs[0,0].legend(fontsize=6);axs[0,1].legend(fontsize=6)
        axs[-1,0].set_xlabel("Virtual time (ms); request leases, not batches")
        axs[-1,1].set_xlabel("Virtual time (ms); GB/s, symmetric-log scale")
        fig.tight_layout();fig.savefig(destination/f"tfs_{workload}_quota_bandwidth.png",dpi=150);plt.close(fig)

    workloads = sorted({s["workload"] for s in summaries})
    fig, axs = plt.subplots(1,len(workloads),figsize=(5*len(workloads),3.8),squeeze=False)
    for ax, workload in zip(axs[0],workloads):
        subset = [s for s in summaries if s["workload"]==workload]
        groups = defaultdict(list)
        for s in subset:
            label=f"{s['mode']}/{s['release_on']}/{s['feedback_delay_s']*1000:g}ms"
            groups[label].append(s)
        for label, vs in sorted(groups.items()):
            vs=sorted(vs,key=lambda s:s["max_num_seqs"])
            ax.plot([s["max_num_seqs"] for s in vs], [s["profile_attainment"]*100 for s in vs],marker="o",label=label)
        ax.set(title=workload,xlabel="Configured max_num_seqs",ylabel="Profile deadline attainment (%)",ylim=(0,105))
        ax.grid(alpha=.2);ax.legend(fontsize=6)
    fig.tight_layout();fig.savefig(destination/"tfs_attainment_by_capacity.png",dpi=150);plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("directory")
    p.add_argument("--figures", default=None)
    a = p.parse_args()
    result = analyze(a.directory, a.figures or str(Path(a.directory) / "figures"))
    print(f"runs={len(result['runs'])}; summaries={len(result['summary'])}")


if __name__ == "__main__":
    main()
