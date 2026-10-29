#!/usr/bin/env python3
"""Replay frozen SA ABC stack inputs in DES with explicit round-robin routing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run_sa_alignment import configuration


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="frozen run_sa_abc_stack preparation directory")
    p.add_argument("--output", required=True, help="new directory; existing output is refused")
    a = p.parse_args()
    source, out = Path(a.source).resolve(), Path(a.output).resolve()
    base = json.loads((source / "config.json").read_text())
    trace = [json.loads(line) for line in (source / "trace.jsonl").read_text().splitlines()]
    rows = [dict(id=r["id"], sa_rid=r["sa_rid"], arrival_s=r["arrival_s"],
                 class_id=r["class_id"], h_tokens=r["hit_tokens"], u_tokens=r["u_tokens"],
                 T0_s=r["T0_s"], deadline_s=r["deadline_s"]) for r in trace]
    metadata = json.loads((ROOT / "results/sa_alignment_20261019/source/source.json").read_text())
    out.mkdir(parents=True, exist_ok=False)
    trace_path = out / "trace.json"
    trace_path.write_text(json.dumps(rows, indent=2) + "\n")
    normalized = []
    for mode in ("immediate", "tfs"):
        c = configuration(metadata, trace_path, base["max_num_seqs"], mode)
        c.update(workers=base["workers"], router_mode="round-robin")
        c["ttft_slo_s"] = base["ttft_slo_s"]
        c["admission"] = {**base["admission"], "mode": mode}
        for key in ("workers", "max_num_seqs", "max_num_batched_tokens", "num_blocks",
                    "block_size", "max_model_len", "enable_prefix_caching", "policy", "scheduler"):
            assert c[key] == base[key], key
        assert c["sa_profile"] == base["compute"]["profile"]
        for key in ("bandwidth_policy", "shared_link_Bps", "q_max_Bps", "read_latency_s"):
            assert c["storage"][key] == base["storage"][key], key
        assert c["storage"]["layer_bytes_per_token"] * c["sa_profile"]["L"] == base["storage"]["kv_bytes_per_token"]
        folder = out / mode
        folder.mkdir()
        (folder / "config.json").write_text(json.dumps(c, indent=2) + "\n")
        normalized.append({**c, "admission": {**c["admission"], "mode": "paired"}})
    assert normalized[0] == normalized[1]
    snapshot = out / "runtime_sources"
    snapshot.mkdir()
    manifest = {}
    for path in sorted((ROOT / "sim").glob("*.py")) + [Path(__file__).resolve(), ROOT / "scripts/check_sa_results.py"]:
        target = snapshot / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest[str(path.relative_to(ROOT))] = sha(path)
    (out / "provenance.json").write_text(json.dumps(dict(
        source=str(source), source_trace_sha256=sha(source / "trace.jsonl"),
        trace_sha256=sha(trace_path), source_config_sha256=sha(source / "config.json"),
        requests=len(rows), bytes=sum(r["h_tokens"] for r in rows) * base["storage"]["kv_bytes_per_token"],
        numerical_inputs="Exact planned arrival, h/u, class, T0 and deadline from frozen stack input",
        token_metadata="Legacy DES synthetic IDs; local cache off and cache-blind routing",
        routing="Python cyclic selection 0..N-1; native Dynamo create_reservation/prefill_complete/free",
        source_sha256=manifest,
    ), indent=2) + "\n")
    env = {**os.environ, "PYTHONHASHSEED": "0", "VLLM_PLUGINS": "", "HF_HUB_OFFLINE": "1",
           "OMP_NUM_THREADS": "1", "VLLM_LOGGING_LEVEL": "ERROR", "VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1"}
    for mode in ("immediate", "tfs"):
        folder = out / mode
        with (folder / "run.log").open("w") as log:
            subprocess.run([sys.executable, "-m", "sim.sa_aligned", "--config", str(folder / "config.json"),
                            "--trace", str(trace_path), "--output", str(folder)], cwd=ROOT,
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        with (folder / "check.log").open("w") as log:
            subprocess.run([sys.executable, "scripts/check_sa_results.py", str(folder)], cwd=ROOT,
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        r = json.loads((folder / "report.json").read_text())
        print(mode, json.dumps({key: r[key] for key in ("requests_completed", "makespan_s", "mean_ttft_s",
                                                      "profile_deadline_attainment", "bytes_read")}), flush=True)


if __name__ == "__main__":
    main()
