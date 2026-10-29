#!/usr/bin/env python3
"""展开固定矩阵，每格独立进程；同 workload/seed 的 trace 只生成一次。"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.gen_tfs_trace import PRESETS, write_trace
from sim.request_profile import normalize_config

ROOT = Path(__file__).resolve().parents[1]


def assign(config, dotted, value):
    parts = dotted.split(".")
    obj = config
    for key in parts[:-1]:
        obj = obj.setdefault(key, {})
    obj[parts[-1]] = value


def expand(spec_path, output):
    """base → workload 固定配置 → fixed → axes；路径相对 spec。"""
    spec_path, output = Path(spec_path).resolve(), Path(output).resolve()
    spec = json.loads(spec_path.read_text())
    if spec.get("schema_version") != 1:
        raise ValueError("unknown matrix schema")
    fixed, axes = spec.get("fixed", {}), spec["axes"]
    if set(fixed) & set(axes):
        raise ValueError("a key appears in both fixed and axes")
    if not spec["seeds"] or spec.get("repeats_per_trace", 1) < 1:
        raise ValueError("empty seeds or repeats")
    base = json.loads((spec_path.parent / spec["base_config"]).read_text())
    jobs = []
    for workload in spec["workloads"]:
        name = workload["name"]
        if not name or any(x in name for x in ("/", "\\", "..")):
            raise ValueError("unsafe workload name")
        params = dict(PRESETS[workload["generator_preset"]])
        params.update(workload.get("generator_parameters", {}))
        for seed in spec["seeds"]:
            trace = output / "traces" / f"{name}-s{seed}.jsonl"
            if "trace" in workload:
                trace = (spec_path.parent / workload["trace"].format(seed=seed)).resolve()
                if not trace.is_file():
                    raise ValueError(f"missing trace: {trace}")
            else:
                write_trace(trace, seed, params)
            for values in itertools.product(*axes.values()):
                for repeat in range(spec.get("repeats_per_trace", 1)):
                    c = json.loads(json.dumps(base))
                    for key, value in workload.get("fixed", {}).items():
                        assign(c, key, value)
                    for key, value in fixed.items():
                        assign(c, key, value)
                    for key, value in zip(axes, values):
                        assign(c, key, value)
                    c = normalize_config(c)
                    tokens = [f"{key.replace('.', '-')}-{str(v).lower()}" for key, v in zip(axes, values)]
                    directory = output / "runs" / name / f"s{seed}-r{repeat}" / "__".join(tokens)
                    directory.mkdir(parents=True, exist_ok=True)
                    config_path = directory / "config.json"
                    config_path.write_text(json.dumps(c, indent=2) + "\n")
                    provenance = {"workload": name, "seed": seed, "repeat": repeat,
                                  "generator_preset": workload["generator_preset"],
                                  "generator_parameters": params, "trace": str(trace),
                                  "trace_file_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
                                  "axes": dict(zip(axes, values)), "config": str(config_path),
                                  "output": str(directory)}
                    (directory / "cell.json").write_text(json.dumps(provenance, indent=2) + "\n")
                    jobs.append(provenance)
    if len({j["output"] for j in jobs}) != len(jobs):
        raise ValueError("duplicate matrix cells")
    output.mkdir(parents=True, exist_ok=True)
    (output / "spec.resolved.json").write_text(json.dumps(spec, indent=2) + "\n")
    (output / "manifest.json").write_text(json.dumps(jobs, indent=2) + "\n")
    return jobs


def run_cell(job, resume):
    directory = Path(job["output"])
    if resume and (directory / "report.json").exists():
        report = json.loads((directory / "report.json").read_text())
        if report.get("implementation_sha256") == hashlib.sha256(b"".join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/"sim").glob("*.py")))).hexdigest() and report.get("status") == "completed" and report["config"] == json.loads(Path(job["config"]).read_text()):
            # 配置与规范化 trace 哈希均一致才可复用。
            rows = sorted([json.loads(x) for x in Path(job["trace"]).read_text().splitlines() if x], key=lambda r: (r["arrival_s"], r["id"]))
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if digest == report["workload_sha256"] and (directory / "events.jsonl").is_file():
                with (directory / "check.log").open("w") as log:
                    checked = subprocess.run([sys.executable, str(ROOT / "scripts/check_results.py"), str(directory)],
                                             cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                if checked.returncode == 0:
                    return {"output": job["output"], "status": "resumed"}
    for name in ("report.json", "events.jsonl"):
        (directory / name).unlink(missing_ok=True)
    env = {**os.environ, "PYTHONHASHSEED": "0", "VLLM_PLUGINS": "", "HF_HUB_OFFLINE": "1",
           "VLLM_LOGGING_LEVEL": "ERROR", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    start = time.monotonic()
    with (directory / "run.log").open("w") as log:
        result = subprocess.run([sys.executable, "-m", "sim.run", "--config", job["config"],
                                 "--trace", job["trace"], "--output", job["output"]],
                                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode == 0:
        with (directory / "check.log").open("w") as log:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/check_results.py"), str(directory)],
                                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    status = {"output": job["output"], "returncode": result.returncode,
              "wall_s": time.monotonic() - start, "status": "completed" if result.returncode == 0 else "failed"}
    (directory / "execution.json").write_text(json.dumps(status, indent=2) + "\n")
    return status


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--spec", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--jobs", type=int, default=2)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.jobs < 1:
        p.error("--jobs must be positive")
    jobs = expand(a.spec, a.output)
    print(f"matrix cells={len(jobs)}", flush=True)
    if a.dry_run:
        return
    results = []
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        fs = [pool.submit(run_cell, j, a.resume) for j in jobs]
        for future in as_completed(fs):
            result = future.result()
            results.append(result)
            if len(results) % 20 == 0 or result["status"] == "failed":
                print(f"finished={len(results)}/{len(jobs)} last={result['status']}", flush=True)
    (Path(a.output) / "execution.json").write_text(json.dumps(results, indent=2) + "\n")
    if any(r["status"] == "failed" for r in results):
        raise SystemExit("matrix contains failures; inspect run.log")


if __name__ == "__main__":
    main()
