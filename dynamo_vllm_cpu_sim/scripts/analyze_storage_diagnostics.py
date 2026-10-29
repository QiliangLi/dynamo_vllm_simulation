#!/usr/bin/env python3
"""Summarize saved storage-only timings and run a separate virtual-time control."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.diagnose_storage_submit import prepare, summary_ms  # noqa: E402


def analyze(directory):
    report = json.loads((directory / "report.json").read_text())
    for path, digest in report["source_sha256"].items():
        assert hashlib.sha256((directory / "runtime_sources" / path).read_bytes()).hexdigest() == digest
        if path.startswith("sim/"):
            assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
    grouped = defaultdict(list)
    for row in report["trials"]:
        grouped[row["case"]].append(row)
    cases = {}
    for name, rows in grouped.items():
        metrics = sorted({key for row in rows for key in row["metrics"]})
        cases[name] = dict(repeats=len(rows), peaks=[r["peak_pending"] for r in rows],
            metrics={key: dict(
                median_of_run_medians_ms=statistics.median(
                    r["metrics"][key]["median_ms"] for r in rows if key in r["metrics"]),
                run_medians_ms=[r["metrics"][key]["median_ms"] for r in rows if key in r["metrics"]])
                for key in metrics})
    segments = defaultdict(lambda: defaultdict(list))
    seen = set()
    for line in (directory / "events.jsonl").read_text().splitlines():
        trial = json.loads(line)
        key = (trial["case"], trial["repeat"])
        assert key not in seen
        seen.add(key)
        servers = {(r["worker"], r["sequence"]): r for r in trial["server_rows"]
                   if r["op"] == "submit"}
        for client in trial.get("clients", []):
            for call in client["calls"]:
                if call["op"] != "submit":
                    continue
                server = servers[(call["worker"], call["sequence"])]
                assert call["called_ns"] <= server["framed_ns"] <= server["response_encoded_ns"] <= call["decoded_ns"]
                stages = segments[trial["case"]]
                stages["encode_to_server_userspace_frame"].append(
                    (server["framed_ns"] - call["encoded_ns"]) / 1e6)
                stages["response_encode_to_client_decode"].append(
                    (call["decoded_ns"] - server["response_encoded_ns"]) / 1e6)
                operation = server["operation_done_ns"] - server["acquired_ns"]
                stages["operation"].append(operation / 1e6)
                stages["advance"].append(server["advance_ns"] / 1e6)
                stages["model_submit"].append(server["model_submit_ns"] / 1e6)
                stages["other_operation"].append(
                    (operation - server["advance_ns"] - server["model_submit_ns"]) / 1e6)
    assert len(seen) == len(report["trials"])
    cfg = json.loads((directory / "config.json").read_text())
    service, specs, demand = prepare(cfg, report["workers"], report["blocks"], 0)
    for spec in specs:
        service.store.submit(0., (spec["worker"], spec["rid"]),
                             [bytes.fromhex(h) for h in spec["blocks_hex"]], demand)
    peak = len(service.store.pending)
    while service.store.pending:
        service.store.advance(service.store.next_time())
    expected = report["workers"] * report["blocks"] * cfg["storage"]["kv_bytes_per_token"]
    assert abs(service.store.bytes_transferred - expected) < 1
    assert peak == report["workers"]
    result = dict(analyzer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        report_sha256=hashlib.sha256((directory / "report.json").read_bytes()).hexdigest(),
        events_sha256=hashlib.sha256((directory / "events.jsonl").read_bytes()).hexdigest(),
        aggregation="Main table: median across run medians. Segments: pooled per-request samples.",
        cases=cases, segments={case: {key: dict(summary_ms(values), mean_ms=statistics.mean(values))
                                    for key, values in metrics.items()}
                              for case, metrics in segments.items()},
        virtual_control=dict(peak_pending=peak, finish_ms=service.store.now * 1000,
                             bytes_transferred=service.store.bytes_transferred),
        archived=report.get("archived"),
        limitations=["Storage-only synthetic trial, not a full-stack rerun.",
            "Userspace frame timestamps cannot identify kernel arrival or GIL waiting separately.",
            "Detailed timers and profiling perturb execution; retain baseline and run variation.",
            "Archived full-stack data cannot apportion missing RPC phase timings."])
    (directory / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    columns = ["case", "peaks", "operation", "advance", "model_submit", "other_operation",
               "submit_call", "query_call", "model_read", "model_done_to_client"]
    with (directory / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns[:2] + [k + "_ms" for k in columns[2:]],
                                lineterminator="\n")
        writer.writeheader()
        for name, case in cases.items():
            writer.writerow(dict(case=name, peaks="/".join(map(str, case["peaks"])),
                **{key + "_ms": case["metrics"].get(key, {}).get("median_of_run_medians_ms")
                   for key in columns[2:]}))
    print(json.dumps(dict(virtual_control=result["virtual_control"], trials=len(seen),
                         cases=len(cases)), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    analyze(parser.parse_args().directory)
