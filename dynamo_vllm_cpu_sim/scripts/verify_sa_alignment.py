#!/usr/bin/env python3
"""八个真实路由/调度运行与独立 SA CqEngine 真值逐请求对拍。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_sa_alignment import configuration

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    a = p.parse_args()
    output = Path(a.output).resolve()
    source = json.loads((output / "source/source.json").read_text())
    cases = []
    for row in source["singleton_truth"]:
        cases.append(
            (
                "single-" + row["class_id"],
                [
                    {
                        "id": "r00000",
                        "sa_rid": 0,
                        "arrival_s": 0.0,
                        "class_id": row["class_id"],
                        "h_tokens": row["h_tokens"],
                        "u_tokens": row["u_tokens"],
                        "T0_s": row["T0_s"],
                        "deadline_s": 4 * row["T0_s"],
                    }
                ],
                [row["finish_s"]],
            )
        )
    for case in source["serial_truth"] + source["parallel_truth"]:
        rows = [
            {
                "id": f"r{r['rid']:05d}",
                "sa_rid": r["rid"],
                "arrival_s": r["arrival_s"],
                "class_id": r["class_id"],
                "h_tokens": r["h_tokens"],
                "u_tokens": r["u_tokens"],
                "T0_s": r["T0_s"],
                "deadline_s": r["deadline_s"],
            }
            for r in case["requests"]
        ]
        cases.append(
            (
                case.get("kind", "serial") + "-" + case["variant"],
                rows,
                [r["F_s"] for r in case["requests"]],
            )
        )
    results = []
    for name, rows, truth in cases:
        folder = output / "verification" / name
        folder.mkdir(parents=True, exist_ok=True)
        trace = folder / "trace.json"
        trace.write_text(json.dumps(rows) + "\n")
        cfg = configuration(source, trace, 1, "immediate")
        cfg.update(workers=2 if name.startswith("parallel-") else 1, num_blocks=50001)
        (folder / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        env = {
            **os.environ,
            "OMP_NUM_THREADS": "1",
            "VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1",
        }
        with (folder / "run.log").open("w") as log:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sim.sa_aligned",
                    "--config",
                    str(folder / "config.json"),
                    "--trace",
                    str(trace),
                    "--output",
                    str(folder),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        with (folder / "check.log").open("w") as log:
            subprocess.run(
                [sys.executable, "scripts/check_sa_results.py", str(folder)],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        report = json.loads((folder / "report.json").read_text())
        actual = [report["requests"][r["id"]]["first_token_s"] for r in rows]
        error = max(abs(a - b) for a, b in zip(actual, truth))
        assert error <= 1e-10, (name, actual, truth)
        results.append(
            {
                "case": name,
                "sa_finish_s": truth,
                "dv_finish_s": actual,
                "max_abs_error_s": error,
                "passed": True,
            }
        )
        print(name, "SA engine parity PASS", error, flush=True)
    (output / "verification.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
