#!/usr/bin/env python3
"""Scale frozen E26f arrivals to finite-window storage input load; run DES pairs."""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_sa_alignment import configuration
from sim.sa_model import reference


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scale_rows(rows, profile, bandwidth, rho, qmax):
    if not rows or min(bandwidth, rho, qmax) <= 0 or rho > 1:
        raise ValueError("positive bandwidth/qmax and 0 < rho <= 1 required")
    size = sum(r["h_tokens"] for r in rows) * 4096 * profile["L"]
    old_end = max(r["arrival_s"] for r in rows)
    if size <= 0 or old_end <= 0:
        raise ValueError("nonzero input bytes and arrival span required")
    window = float(Fraction(size) / Fraction(str(bandwidth)) / Fraction(str(rho)))
    scale = window / old_end
    output = []
    compute = 0.
    for row in rows:
        _, c, t0 = reference(row, profile, bandwidth, qmax)
        arrival = row["arrival_s"] * scale
        output.append({**row, "arrival_s": arrival, "T0_s": t0,
                       "deadline_s": arrival + 4*t0})
        compute += c * profile["L"]
    return output, dict(total_read_bytes=size, source_last_arrival_s=old_end,
                        arrival_scale=scale, planned_window_s=window,
                        storage_input_load=size/window/bandwidth,
                        singleton_compute_engine_s=compute)


def prepare(source, output, workers, bandwidth, rho, qmax, max_seqs, seeds):
    source, output = Path(source).resolve(), Path(output).resolve()
    if workers < 1 or max_seqs < 1:
        raise ValueError("positive workers and max_num_seqs required")
    metadata = json.loads((source / "source.json").read_text())
    cells = {cell["seed"]: cell for cell in metadata["cells"]}
    if set(seeds) - cells.keys() or len(set(seeds)) != len(seeds):
        raise ValueError("unknown/duplicate input seed")
    output.mkdir(parents=True, exist_ok=False)
    (output / "source").mkdir()
    shutil.copy2(source / "source.json", output / "source/source.json")
    snapshot = output / "runtime_sources"
    paths = [*sorted((ROOT / "sim").glob("*.py")), Path(__file__).resolve(),
             ROOT / "scripts/run_sa_alignment.py", ROOT / "scripts/check_sa_results.py"]
    hashes = {}
    for path in paths:
        dest = snapshot / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        hashes[str(path.relative_to(ROOT))] = digest(path)
    jobs, inputs = [], []
    for seed in seeds:
        cell = cells[seed]
        path = source / cell["trace"]
        assert digest(path) == cell["trace_sha256"]
        rows = json.loads(path.read_text())
        assert Counter(r["class_id"] for r in rows) == Counter(A=120, B=120, C=120)
        shutil.copy2(path, output / "source" / path.name)
        rows, info = scale_rows(rows, metadata["profile"], bandwidth, rho, qmax)
        folder = output / cell["name"]
        folder.mkdir()
        trace = folder / "trace.json"
        trace.write_text(json.dumps(rows, indent=2) + "\n")
        info.update(seed=seed, variant=cell["variant"], requests=len(rows), workers=workers,
                    source_sha256=digest(path), trace_sha256=digest(trace),
                    bandwidth_Bps=bandwidth, q_max_Bps=qmax,
                    compute_input_load=info["singleton_compute_engine_s"]/workers/info["planned_window_s"],
                    note="Finite-window normalization of one frozen random sample; not a new stationary Poisson load")
        inputs.append(info)
        (folder / "input_check.json").write_text(json.dumps(info, indent=2) + "\n")
        for mode in ("immediate", "tfs"):
            c = configuration(metadata, trace, max_seqs, mode)
            c.update(workers=workers, router_mode="round-robin")
            c["storage"].update(shared_link_Bps=bandwidth, q_max_Bps=qmax)
            c["admission"]["reference_bandwidth_Bps"] = bandwidth
            run = folder / mode
            run.mkdir()
            (run / "config.json").write_text(json.dumps(c, indent=2) + "\n")
            jobs.append(dict(seed=seed, variant=cell["variant"], mode=mode,
                             trace=str(trace), output=str(run)))
    manifest = dict(source=str(source), source_metadata_sha256=digest(source / "source.json"),
                    source_sha256=hashes, inputs=inputs, jobs=jobs,
                    scope="Single Python DES process; native vLLM Scheduler/KV manager and native Dynamo reservations; Python round-robin selection")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def run_jobs(output, manifest):
    env = {**os.environ, "PYTHONHASHSEED": "0", "VLLM_PLUGINS": "", "HF_HUB_OFFLINE": "1",
           "OMP_NUM_THREADS": "1", "VLLM_LOGGING_LEVEL": "ERROR", "VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1"}
    for job in manifest["jobs"]:
        folder = Path(job["output"])
        with (folder / "run.log").open("w") as log:
            subprocess.run([sys.executable, "-m", "sim.sa_aligned", "--config", str(folder / "config.json"),
                            "--trace", job["trace"], "--output", str(folder)], cwd=ROOT, env=env,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        with (folder / "check.log").open("w") as log:
            subprocess.run([sys.executable, "scripts/check_sa_results.py", str(folder)], cwd=ROOT,
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        report = json.loads((folder / "report.json").read_text())
        print(json.dumps(dict(seed=job["seed"], mode=job["mode"],
                              requests=report["requests_completed"], mean_ttft_s=report["mean_ttft_s"],
                              slo=report["profile_deadline_attainment"], makespan_s=report["makespan_s"])), flush=True)
    assert all(digest(ROOT / path) == value for path, value in manifest["source_sha256"].items())
    (Path(output) / "completed.json").write_text(json.dumps(dict(status="pass", runs=len(manifest["jobs"]),
                                                              runtime_sources_unchanged=True), indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=ROOT / "results/sa_alignment_20261019/source")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--bandwidth-gbs", type=float, default=60.)
    p.add_argument("--rho", type=float, default=.8)
    p.add_argument("--q-max-gbs", type=float, default=200.)
    p.add_argument("--max-num-seqs", type=int, default=4)
    p.add_argument("--seeds", nargs="+", type=int, default=[416,417,418,424])
    p.add_argument("--prepare-only", action="store_true")
    a = p.parse_args()
    manifest = prepare(a.source, a.output, a.workers, a.bandwidth_gbs*1e9, a.rho,
                       a.q_max_gbs*1e9, a.max_num_seqs, a.seeds)
    print(json.dumps(manifest["inputs"], indent=2), flush=True)
    if not a.prepare_only:
        run_jobs(a.output, manifest)


if __name__ == "__main__":
    main()
