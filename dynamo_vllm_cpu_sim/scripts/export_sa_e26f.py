#!/usr/bin/env python3
"""在独立进程中从固定 SA 源码导出官方输入，并运行 FCFS/TFS 现场控制。"""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sa-root", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    root, output = Path(args.sa_root).resolve(), Path(args.output).resolve()
    sys.path.insert(0, str(root))
    from sim.experiments import e26f_third as e26f
    from sim.experiments import e26_ab as e26
    from sim.cq.policies import make_fcfs, make_tfs
    from sim.cq.simrun import run_case
    from sim.cq.metrics import request_rows, summarize

    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    paths = [
        "sim/experiments/e26f_third.py",
        "sim/experiments/e26_ab.py",
        "sim/cq/profile.py",
        "sim/cq/config.py",
        "sim/cq/storage.py",
        "sim/cq/engine.py",
        "sim/cq/policies.py",
    ]
    provenance = {
        "source_root": str(root),
        "commit": revision,
        "source_sha256": {
            f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in paths
        },
        "profile": e26.E26_PROFILE.to_json(),
        "cells": [],
    }
    scenario = e26.e26_scenario(4)
    scenario = replace(scenario, storage=replace(scenario.storage, alloc="max_min"))
    for variant, seeds in (("A", [416, 417, 418]), ("B", [424])):
        for seed in seeds:
            specs, centers = e26f.d1f_specs(16, seed, e26f.C_CLASSES[variant])
            rows = [
                {
                    "id": f"r{s.rid:05d}",
                    "sa_rid": s.rid,
                    "class_id": s.class_id,
                    "arrival_s": float(s.arrival_s),
                    "h_tokens": s.h_tokens,
                    "u_tokens": s.u_tokens,
                    "T0_s": float(s.T0_s),
                    "deadline_s": float(s.deadline_s),
                }
                for s in specs
            ]
            name = f"D1f{variant}-s{seed}"
            trace = output / f"{name}.json"
            trace.write_text(json.dumps(rows, separators=(",", ":")) + "\n")
            controls = {}
            for mode, factory in (("fcfs", make_fcfs), ("tfs", make_tfs)):
                eng = run_case(
                    scenario, specs, factory(), seed=seed, record_intervals=True
                )
                if eng.status != "done":
                    raise RuntimeError(f"SA {name}/{mode}: {eng.status}")
                result = {
                    "summary": summarize(eng, scenario),
                    "requests": request_rows(eng),
                    "classes": e26f.class_metrics3(eng, specs),
                    "storage_bytes": float(eng.w.storage.actual_integral_gb) * 1e9,
                    "worker_count": len(eng.w.workers),
                    "decision_count": len(eng.decisions),
                }
                controls[mode] = result
                (output / f"{name}-{mode}.json").write_text(
                    json.dumps(result, indent=2, default=float) + "\n"
                )
                print(name, mode, "SA completed", flush=True)
            provenance["cells"].append(
                {
                    "name": name,
                    "variant": variant,
                    "seed": seed,
                    "trace": trace.name,
                    "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
                    "a_centers": centers,
                    "controls": {m: f"{name}-{m}.json" for m in controls},
                }
            )
    # 单请求真值：同一个 SA 事件引擎运行，供新执行模型做非循环自证的对拍。
    from sim.cq.types import RequestSpec
    from sim.cq.profile import make_T0
    from fractions import Fraction as F

    singles = []
    one = replace(scenario, m_workers=1)
    for cls, (h, u) in (
        ("A", e26.A_CLASS),
        ("B", e26.B_CLASS),
        ("C_A", e26f.C_A_CLASS),
        ("C_B", e26f.C_B_CLASS),
    ):
        rs = RequestSpec(0, 0.0, h, u, cls, F(1), F(1))
        t0 = make_T0([rs], one.profile, F(120), F(200))[0]
        rs = replace(rs, T0_s=t0, deadline_s=4 * t0)
        eng = run_case(one, [rs], make_fcfs(), record_intervals=True)
        singles.append(
            {
                "class_id": cls,
                "h_tokens": h,
                "u_tokens": u,
                "T0_s": float(t0),
                "finish_s": float(eng.w.requests[0].F_s),
                "storage_bytes": float(eng.w.storage.actual_integral_gb) * 1e9,
            }
        )
    provenance["singleton_truth"] = singles
    serial_truth = []
    for variant in ("A", "B"):
        specs = []
        for i, (h, u) in enumerate(
            [e26.A_CLASS, e26.B_CLASS, e26f.C_CLASSES[variant]] * 2
        ):
            rs = RequestSpec(i, 0.0, h, u, "ABC"[i % 3], F(1), F(1))
            t0 = make_T0([rs], one.profile, F(120), F(200))[i]
            specs.append(replace(rs, T0_s=t0, deadline_s=4 * t0))
        eng = run_case(one, specs, make_fcfs(), record_intervals=True)
        serial_truth.append(
            {
                "variant": variant,
                "requests": request_rows(eng),
                "storage_bytes": float(eng.w.storage.actual_integral_gb) * 1e9,
            }
        )
    provenance["serial_truth"] = serial_truth
    parallel_truth = []
    two = replace(scenario, m_workers=2)
    for labels in ("AA", "AB"):
        specs = []
        for i, label in enumerate(labels):
            h, u = e26.A_CLASS if label == "A" else e26.B_CLASS
            rs = RequestSpec(i, 0.0, h, u, label, F(1), F(1))
            t0 = make_T0([rs], two.profile, F(120), F(200))[i]
            specs.append(replace(rs, T0_s=t0, deadline_s=4 * t0))
        eng = run_case(two, specs, make_fcfs(), record_intervals=True)
        parallel_truth.append(
            {
                "variant": labels,
                "kind": "parallel",
                "requests": request_rows(eng),
                "storage_bytes": float(eng.w.storage.actual_integral_gb) * 1e9,
            }
        )
    provenance["parallel_truth"] = parallel_truth
    provenance["scenario"] = scenario.to_json()
    (output / "source.json").write_text(
        json.dumps(provenance, indent=2, default=float) + "\n"
    )


if __name__ == "__main__":
    main()
