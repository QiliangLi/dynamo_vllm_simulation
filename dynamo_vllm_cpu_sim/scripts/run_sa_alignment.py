#!/usr/bin/env python3
"""冻结 SA 对齐配置、生成独立格点、顺序运行并逐格验收；不调参/不重试。"""

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def configuration(source, trace, cap, mode):
    memory = {
        "total_bytes": 8e9,
        "base_bytes": 0.5e9,
        "kv_bytes_per_token": 8 * 4096,
        "workspace_bytes_per_token": 16 * 4096 * 2,
    }
    blocks = (
        int(
            (
                memory["total_bytes"]
                - memory["base_bytes"]
                - 8192 * memory["workspace_bytes_per_token"]
            )
            / memory["kv_bytes_per_token"]
        )
        + 1
    )
    return {
        "workers": 32,
        "scheduler": "upstream",
        "policy": "fcfs",
        "block_size": 1,
        "connector_name": "SALayerConnector",
        "num_blocks": blocks,
        "max_model_len": 50000,
        "max_num_seqs": cap,
        "max_num_batched_tokens": 8192,
        "enable_prefix_caching": False,
        "memory": memory,
        "sa_profile": source["profile"],
        "source_trace": str(trace.resolve()),
        "source_commit": source["commit"],
        "source_sha256": source["source_sha256"],
        "storage": {
            "bandwidth_policy": "sa_max_min",
            "shared_link_Bps": 120e9,
            "q_max_Bps": 200e9,
            "layer_bytes_per_token": 4096,
            "read_latency_s": 0,
        },
        "ttft_slo_s": 1.0,
        "admission": {
            "mode": mode,
            "quantile": 0.75,
            "reference_bandwidth_Bps": 120e9,
            "release_on": "first_token",
            "aging_enabled": False,
            "aging_wait_s": 0,
            "aging_cooldown_s": 0.05,
            "feedback_delay_s": 0,
        },
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--caps", nargs="+", type=int, default=[1, 8])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument(
        "--modes", nargs="+", default=["immediate", "slack_only", "quota_fcfs", "tfs"]
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    a = p.parse_args()
    output = Path(a.output).resolve()
    source = json.loads((output / "source/source.json").read_text())
    impl = hashlib.sha256(
        b"".join(
            f.name.encode() + f.read_bytes()
            for f in sorted((ROOT / "sim").glob("*.py"))
        )
    ).hexdigest()
    jobs = []
    for cell, cap, mode, repeat in itertools.product(
        source["cells"], a.caps, a.modes, range(a.repeats)
    ):
        trace = output / "source" / cell["trace"]
        assert hashlib.sha256(trace.read_bytes()).hexdigest() == cell["trace_sha256"]
        folder = output / "runs" / cell["name"] / f"cap{cap}-{mode}-r{repeat}"
        folder.mkdir(parents=True, exist_ok=True)
        cfg = configuration(source, trace, cap, mode)
        (folder / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        job = {
            "cell": cell["name"],
            "variant": cell["variant"],
            "seed": cell["seed"],
            "cap": cap,
            "mode": mode,
            "repeat": repeat,
            "trace": str(trace),
            "output": str(folder),
            "implementation_sha256": impl,
            "config": cfg,
        }
        jobs.append(job)
    (output / "manifest.json").write_text(json.dumps(jobs, indent=2) + "\n")
    print(f"frozen matrix={len(jobs)} implementation={impl}", flush=True)
    if a.dry_run:
        return
    env = {
        **os.environ,
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "VLLM_PLUGINS": "",
        "HF_HUB_OFFLINE": "1",
        "PYTHONHASHSEED": "0",
        "VLLM_LOGGING_LEVEL": "ERROR",
        "VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1",
    }
    for i, job in enumerate(jobs):
        folder = Path(job["output"])
        start = time.monotonic()
        resume = False
        if a.resume and (folder / "report.json").exists():
            report = json.loads((folder / "report.json").read_text())
            resume = (
                report.get("implementation_sha256") == impl
                and report["config"] == job["config"]
            )
        if not resume:
            (folder / "report.json").unlink(missing_ok=True)
            (folder / "events.jsonl").unlink(missing_ok=True)
            with (folder / "run.log").open("w") as log:
                run = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "sim.sa_aligned",
                        "--config",
                        str(folder / "config.json"),
                        "--trace",
                        job["trace"],
                        "--output",
                        str(folder),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            if run.returncode:
                raise SystemExit(f"failed {folder}; see run.log")
        with (folder / "check.log").open("w") as log:
            checked = subprocess.run(
                [sys.executable, "scripts/check_sa_results.py", str(folder)],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        if checked.returncode:
            raise SystemExit(f"invalid {folder}; see check.log")
        (folder / "execution.json").write_text(
            json.dumps(
                {"wall_s": time.monotonic() - start, "resumed": resume, "passed": True}
            )
            + "\n"
        )
        print(
            f"{i + 1}/{len(jobs)} {job['cell']} cap={job['cap']} {job['mode']} rep={job['repeat']} passed",
            flush=True,
        )


if __name__ == "__main__":
    main()
