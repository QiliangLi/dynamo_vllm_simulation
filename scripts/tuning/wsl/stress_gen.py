#!/usr/bin/env python
"""Derive the stress workloads from the frozen inputs (design section 9, G4).

Runs inside WSL with the package venv. Each stress workload is written as
``<out>/<name>/input/{config.json,trace.jsonl}`` and ``<out>/<name>/bench/{manifest.json,requests.csv}``,
which is what scripts/tuning/wsl/run_tuning.py needs for a prebuilt input. The
frozen benchmark directories are never modified.

A "mix" takes the requests of one class (or two) out of ONE frozen trace and
re-spaces them uniformly. It must not mix several traces: requests of different
synthetic traces share token prefixes, so a request from another trace can store
a longer prefix than the trace says this request hits, and the stack's own check
``estimated_remote_tokens == remote_prefix_tokens`` then fails.
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

SPECS = [
    ("bw10_540", dict(kind="bandwidth", source="540", factor=0.4)),
    ("bw50_540", dict(kind="bandwidth", source="540", factor=2.0)),
    ("bw10_agentx", dict(kind="bandwidth", source="agentx-narrow", factor=0.4)),
    ("arr_fast_540", dict(kind="arrival", source="540", factor=0.5)),
    ("arr_slow_540", dict(kind="arrival", source="540", factor=2.0)),
    ("arr_fast_mooncake", dict(kind="arrival", source="mooncake", factor=0.5)),
    ("heavy_only", dict(kind="mix", source="408", classes=["A"])),
    ("light_only", dict(kind="mix", source="540", classes=["B"])),
    ("zero_read", dict(kind="mix", source="540", classes=["C"])),
    ("bimodal", dict(kind="mix", source="408", classes=["A", "C"])),
    ("workers2_408", dict(kind="workers", source="408", workers=2)),
    ("workers8_540", dict(kind="workers", source="540", workers=8)),
]
BENCH = dict(
    **{"408": "synthetic_abchalfb_408_8s_4_25GBps", "540": "synthetic_abclonghit_540_12s_4_25GBps",
       "mooncake": "mooncake_head875_360_12s_4_25GBps", "agentx-narrow": "agentx_narrow_358_12s_4_25GBps",
       "agentx-flex": "agentx_flex_358_12s_4_25GBps"})
WINDOW = 12.0


def read_rows(path):
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def load(python, workload, tmp):
    """Export one frozen benchmark into ``tmp`` and read its trace and config."""
    out = tmp / workload
    subprocess.run([python, str(REPO / "benchmark" / "manage.py"), "export", BENCH[workload], "--output", str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    bench = REPO / "benchmark" / BENCH[workload]
    return dict(dir=out, rows=read_rows(out / "trace.jsonl"), config=json.loads((out / "config.json").read_text()),
                manifest=json.loads((bench / "manifest.json").read_text()), bench=bench)


def write_bench(folder, name, rows, kv_bytes, window):
    """A benchmark directory in the shape the summary reads: manifest.json and requests.csv."""
    folder.mkdir(parents=True)
    total = 0
    with (folder / "requests.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "class_id", "arrival_s", "input_tokens", "hit_tokens", "new_tokens", "read_bytes"])
        for row in rows:
            hit = row["remote_prefix_tokens"]
            read = hit * kv_bytes
            total += read
            writer.writerow([row["id"], row["class_id"], repr(row["arrival_s"]), len(row["prompt_token_ids"]), hit,
                             len(row["prompt_token_ids"]) - hit, read])
    (folder / "manifest.json").write_text(json.dumps(dict(
        case="stress_" + name, requests=len(rows), read_bytes=total, arrival_window_s=window,
        derived_from="frozen four-worker benchmarks",
        note="stress workload of the tuning workflow; not a published benchmark"), ensure_ascii=False, indent=2) + "\n")


def build(name, spec, sources, out, kv_bytes):
    root = out / name
    if root.exists():
        shutil.rmtree(root)
    (root / "input").mkdir(parents=True)
    kind = spec["kind"]
    if kind == "workers":
        shutil.copytree(sources[spec["source"]]["dir"], root / "input", dirs_exist_ok=True)
        shutil.copytree(sources[spec["source"]]["bench"], root / "bench")
        return
    source = sources[spec["source"]]
    config, rows = json.loads(json.dumps(source["config"])), [dict(r) for r in source["rows"]]
    if kind == "bandwidth":
        for key in ("disk_Bps", "shared_link_Bps"):
            config["storage"][key] = round(config["storage"][key] * spec["factor"], 3)
        config["admission"]["reference_bandwidth_Bps"] = round(config["admission"]["reference_bandwidth_Bps"] * spec["factor"], 3)
        window = source["manifest"]["arrival_window_s"]
    elif kind == "arrival":
        for row in rows:
            row["arrival_s"] = max(0.002, row["arrival_s"] * spec["factor"])
        window = max(row["arrival_s"] for row in rows)
    elif kind == "mix":
        rows = sorted((row for row in rows if row["class_id"] in spec["classes"]), key=lambda row: row["arrival_s"])
        for index, row in enumerate(rows):
            row["arrival_s"] = 0.01 + (WINDOW - 0.02) * index / max(1, len(rows) - 1)
        window = WINDOW
    else:
        raise ValueError("unknown stress kind %r" % kind)
    (root / "input" / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    write_rows(root / "input" / "trace.jsonl", rows)
    write_bench(root / "bench", name, rows, kv_bytes, window)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    with tempfile.TemporaryDirectory(prefix="tuning-stress-") as tmp:
        tmp = Path(tmp)
        sources = {workload: load(sys.executable, workload, tmp)
                   for workload in sorted({spec["source"] for _, spec in SPECS if "source" in spec})}
        kv_bytes = sources["540"]["config"]["storage"]["kv_bytes_per_token"]
        built = []
        for name, spec in SPECS:
            build(name, spec, sources, out, kv_bytes)
            built.append("%s(%d)" % (name, read_rows(out / name / "input" / "trace.jsonl").__len__()))
    print("STRESS_OK " + " ".join(built))
    return 0


if __name__ == "__main__":
    sys.exit(main())
