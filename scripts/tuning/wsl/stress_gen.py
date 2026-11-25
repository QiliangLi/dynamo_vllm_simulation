#!/usr/bin/env python
"""Derive the stress workloads from the frozen inputs (design section 9, G4).

Runs inside WSL with the package venv. Each stress workload is written as
``<out>/<name>/input/{config.json,trace.jsonl}`` and ``<out>/<name>/bench/{manifest.json,requests.csv}``,
which is what scripts/tuning/wsl/run_tuning.py needs for a prebuilt input. The
frozen benchmark directories are never modified.
"""
import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts" / "tuning"))
sys.path.insert(0, str(REPO / "dynamo_vllm_cpu_sim"))

SPECS = [
    ("bw10_540", dict(kind="bandwidth", source="540", factor=0.4)),
    ("bw50_540", dict(kind="bandwidth", source="540", factor=2.0)),
    ("bw10_agentx", dict(kind="bandwidth", source="agentx-narrow", factor=0.4)),
    ("arr_fast_540", dict(kind="arrival", source="540", factor=0.5)),
    ("arr_slow_540", dict(kind="arrival", source="540", factor=2.0)),
    ("arr_fast_mooncake", dict(kind="arrival", source="mooncake", factor=0.5)),
    ("heavy_only", dict(kind="mix", rule="heavy")),
    ("light_only", dict(kind="mix", rule="light")),
    ("zero_read", dict(kind="mix", rule="zero")),
    ("bimodal", dict(kind="mix", rule="bimodal")),
    ("workers2_408", dict(kind="workers", source="408", workers=2)),
    ("workers8_540", dict(kind="workers", source="540", workers=8)),
]
BENCH = dict(
    **{"408": "synthetic_abchalfb_408_8s_4_25GBps", "540": "synthetic_abclonghit_540_12s_4_25GBps",
       "mooncake": "mooncake_head875_360_12s_4_25GBps", "agentx-narrow": "agentx_narrow_358_12s_4_25GBps",
       "agentx-flex": "agentx_flex_358_12s_4_25GBps"})
MIX_SOURCES = ("408", "540", "mooncake")
MIX_COUNT = 300
WINDOW = 12.0


def read_rows(path):
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def load(python, workload, tmp):
    """Export one frozen benchmark into ``tmp`` and read its trace, config and benchmark table."""
    out = tmp / workload
    import subprocess
    subprocess.run([python, str(REPO / "benchmark" / "manage.py"), "export", BENCH[workload], "--output", str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    bench = REPO / "benchmark" / BENCH[workload]
    with (bench / "requests.csv").open(encoding="utf-8-sig") as stream:
        table = {row["id"]: row for row in csv.DictReader(stream)}
    return dict(rows=read_rows(out / "trace.jsonl"), config=json.loads((out / "config.json").read_text()),
                bench=bench, table=table)


def write_bench(folder, name, rows, kv_bytes, window):
    """A benchmark directory in the shape summary.py reads: manifest.json and requests.csv."""
    folder.mkdir(parents=True)
    total = 0
    with (folder / "requests.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "class_id", "arrival_s", "input_tokens", "hit_tokens", "new_tokens", "read_bytes"])
        for row in rows:
            hit = row["remote_prefix_tokens"]
            new = len(row["prompt_token_ids"]) - hit
            read = hit * kv_bytes
            total += read
            writer.writerow([row["id"], row["class_id"], repr(row["arrival_s"]), len(row["prompt_token_ids"]), hit, new, read])
    (folder / "manifest.json").write_text(json.dumps(dict(
        case="stress_" + name, requests=len(rows), read_bytes=total, arrival_window_s=window,
        derived_from="frozen four-worker benchmarks", note="stress workload of the tuning workflow; not a published benchmark"),
        ensure_ascii=False, indent=2) + "\n")


def build(name, spec, sources, out, kv_bytes, pressure_of):
    root = out / name
    if root.exists():
        shutil.rmtree(root)
    (root / "input").mkdir(parents=True)
    if spec["kind"] == "workers":
        src = sources[spec["source"]]
        shutil.copytree(src["dir"], root / "input", dirs_exist_ok=True)
        shutil.copytree(src["bench"], root / "bench")
        return
    if spec["kind"] in ("bandwidth", "arrival"):
        src = sources[spec["source"]]
        config, rows = json.loads(json.dumps(src["config"])), [dict(r) for r in src["rows"]]
        factor = spec["factor"]
        if spec["kind"] == "bandwidth":
            for key in ("disk_Bps", "shared_link_Bps"):
                config["storage"][key] = round(config["storage"][key] * factor, 3)
            config["admission"]["reference_bandwidth_Bps"] = round(config["admission"]["reference_bandwidth_Bps"] * factor, 3)
            window = src["manifest"]["arrival_window_s"]
        else:
            for row in rows:
                row["arrival_s"] = max(0.002, row["arrival_s"] * factor)
            window = max(row["arrival_s"] for row in rows)
        (root / "input" / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        write_rows(root / "input" / "trace.jsonl", rows)
        write_bench(root / "bench", name, rows, kv_bytes, window)
        return
    # mix: take requests from the frozen pools by their peak demand, re-space them uniformly.
    pool = []
    for workload in MIX_SOURCES:
        for row in sources[workload]["rows"]:
            pool.append((pressure_of(row), workload, row))
    positive = sorted((item for item in pool if item[0] > 0), key=lambda item: item[0])
    zero = [item for item in pool if item[0] <= 0]
    rule = spec["rule"]
    if rule == "heavy":
        chosen = positive[-MIX_COUNT:]
    elif rule == "light":
        chosen = positive[:MIX_COUNT]
    elif rule == "zero":
        chosen = zero[:MIX_COUNT]
    else:
        half = MIX_COUNT // 2
        chosen = positive[-half:] + zero[:half]
    rows = []
    for index, (_, workload, row) in enumerate(chosen):
        clone = dict(row, id="m%s_%s" % (workload.replace("-", ""), row["id"]),
                     arrival_s=0.01 + (WINDOW - 0.02) * index / max(1, len(chosen) - 1))
        rows.append(clone)
    (root / "input" / "config.json").write_text(json.dumps(sources["540"]["config"], ensure_ascii=False, indent=2) + "\n")
    write_rows(root / "input" / "trace.jsonl", rows)
    write_bench(root / "bench", name, rows, kv_bytes, WINDOW)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    python = sys.executable
    from minimax_prefill import PrefillTimeModel
    model = PrefillTimeModel()

    def pressure_of(row):
        hit, new = row["remote_prefix_tokens"], len(row["prompt_token_ids"]) - row["remote_prefix_tokens"]
        if not hit:
            return 0.0
        return hit * 65536 / model.estimate(hit, new).compute_s

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    with tempfile.TemporaryDirectory(prefix="tuning-stress-") as tmp:
        tmp = Path(tmp)
        sources = {}
        for workload in sorted({spec["source"] for _, spec in SPECS if "source" in spec} | set(MIX_SOURCES)):
            data = load(python, workload, tmp)
            data["dir"] = tmp / workload
            data["manifest"] = json.loads((data["bench"] / "manifest.json").read_text())
            sources[workload] = data
        kv_bytes = sources["540"]["config"]["storage"]["kv_bytes_per_token"]
        built = []
        for name, spec in SPECS:
            build(name, spec, sources, out, kv_bytes, pressure_of)
            built.append(name)
    print("STRESS_OK " + " ".join(built))
    return 0


if __name__ == "__main__":
    sys.exit(main())
