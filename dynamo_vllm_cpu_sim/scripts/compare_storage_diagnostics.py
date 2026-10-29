#!/usr/bin/env python3
"""Compare sequential before/after runs; validate their frozen evidence first."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(directory):
    report = json.loads((directory / "report.json").read_text())
    analysis = json.loads((directory / "analysis.json").read_text())
    assert analysis["report_sha256"] == digest(directory / "report.json")
    assert analysis["events_sha256"] == digest(directory / "events.jsonl")
    for relative, expected in report["source_sha256"].items():
        assert digest(directory / "runtime_sources" / relative) == expected
    return report, analysis


def compare(before, after, output):
    old, a = load(before)
    new, b = load(after)
    for key in ("platform", "python", "workers", "blocks", "repeats", "history_requests"):
        assert old[key] == new[key], key
    assert (before / "config.json").read_bytes() == (after / "config.json").read_bytes()
    assert set(a["cases"]) == set(b["cases"])
    assert a["virtual_control"] == b["virtual_control"]
    for relative in ("scripts/diagnose_storage_submit.py", "sim/stack/storage_client.py"):
        assert old["source_sha256"][relative] == new["source_sha256"][relative]
    old_trials = {(r["case"], r["repeat"]): r for r in old["trials"]}
    new_trials = {(r["case"], r["repeat"]): r for r in new["trials"]}
    assert set(old_trials) == set(new_trials)
    for key, x in old_trials.items():
        y = new_trials[key]
        for field in ("detail", "read_log", "history"):
            assert x[field] == y[field]
        assert abs(x["bytes_transferred"] - y["bytes_transferred"]) < 1
    rows = []
    for name, x in a["cases"].items():
        y = b["cases"][name]
        for metric in x["metrics"]:
            prior = x["metrics"][metric]["median_of_run_medians_ms"]
            current = y["metrics"][metric]["median_of_run_medians_ms"]
            rows.append(dict(case=name, metric=metric, before_ms=prior, after_ms=current,
                             decrease_percent=100 * (prior - current) / prior if prior else None,
                             before_run_medians_ms=x["metrics"][metric]["run_medians_ms"],
                             after_run_medians_ms=y["metrics"][metric]["run_medians_ms"]))
    report = dict(
        method="Sequential before/after on the same host; 3 repeats per case, not interleaved A/B. "
               "Main timings are medians of run medians; CPU/OS jitter remains.",
        before=before.name, after=after.name,
        before_report_sha256=digest(before / "report.json"),
        after_report_sha256=digest(after / "report.json"),
        script_sha256=digest(Path(__file__)), virtual_control=b["virtual_control"],
        cases={name: dict(before_peaks=a["cases"][name]["peaks"],
                          after_peaks=b["cases"][name]["peaks"],
                          before_queue_entries=[r["queue_entries"] for r in old["trials"] if r["case"] == name],
                          after_queue_entries=[r["queue_entries"] for r in new["trials"] if r["case"] == name])
               for name in a["cases"]},
        limitations=["Storage-only synthetic benchmark; no full-stack or production timing claim.",
                     "Faster bookkeeping changes wall-clock admission spacing. Model-time equivalence "
                     "does not require wall-clock completion times or peaks to remain equal."],
        metrics=rows)
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (output / "runtime_sources").mkdir()
    (output / "runtime_sources" / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    with (output / "summary.csv").open("w", newline="") as stream:
        columns = ["case", "metric", "before_ms", "after_ms", "decrease_percent"]
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        if row["metric"] in ("operation", "submit_call"):
            print(json.dumps(row))
    print(json.dumps(report["cases"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    compare(args.before, args.after, args.output)
