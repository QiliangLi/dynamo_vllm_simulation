"""Audit native KV-router decisions and load cleanup from unmodified Dynamo logs."""
import argparse
import json
import math
from pathlib import Path
import re

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOAD_PREFIX = "dynamo_frontend_worker_active_"


def metric_values(lines):
    return {line.rsplit(None,1)[0]:float(line.rsplit(None,1)[1]) for line in lines
            if line.startswith(LOAD_PREFIX)}


def metrics_drained(lines):
    values = metric_values(lines)
    return (any("active_prefill_tokens{" in key for key in values)
            and any("active_decode_blocks{" in key for key in values)
            and all(math.isfinite(value) and value == 0 for value in values.values()))


def decisions(text, expected_workers):
    scores, chosen = {}, {}
    for line in ANSI.sub("",text).splitlines():
        request = re.search(r'request_id=(?:"([^\"]+)"|(\S+))',line)
        if request is None:
            continue
        rid = request.group(1) or request.group(2)
        if "Formula for worker_id=" in line:
            worker = re.search(r"Formula for worker_id=(\d+)",line)
            cost = re.search(r"cached blocks: ([0-9.eE+-]+)",line)
            assert worker and cost, line
            wid = int(worker.group(1))
            assert wid not in scores.setdefault(rid,{}), (rid,wid,"duplicate score")
            scores[rid][wid] = float(cost.group(1))
        if "[ROUTING] Best:" in line:
            assert rid not in chosen, (rid,"duplicate selection")
            chosen[rid] = int(re.search(r"Best: worker_(\d+)",line).group(1))
    assert chosen, "no native KV-routing decisions; not enough evidence to accept"
    for rid,wid in chosen.items():
        costs = scores[rid]
        assert len(costs) == expected_workers, (rid,len(costs),expected_workers)
        assert wid in costs and costs[wid] <= min(costs.values())+1e-6, (rid,wid,costs)
    assert set(scores) == set(chosen)
    return dict(decisions=len(chosen),candidate_scores=sum(map(len,scores.values())),
                distinct_native_workers=len(set(chosen.values())),
                unequal_load_decisions=sum(len(set(scores[rid].values()))>1 for rid in chosen))


def check(folder, *, require_sampled_load=True):
    folder = Path(folder)
    cfg = json.loads((folder/"config.json").read_text())
    assert cfg["router_mode"] == "kv"
    launch = json.loads((folder/"routing.launch.json").read_text())
    assert launch["router_mode"] == "kv"
    argv = launch["argv"]
    assert argv[argv.index("--router-mode")+1] == "kv"
    assert "--no-router-kv-events" in argv and "--no-router-assume-kv-reuse" in argv
    assert argv[argv.index("--router-temperature")+1] == "0"
    assert argv[argv.index("--router-kv-overlap-score-credit")+1] == "0"
    result = decisions((folder/"frontend.log").read_text(),cfg["workers"])
    report = json.loads((folder/"report.json").read_text())
    assert result["decisions"] == report["n_total"] == report["n_ok"]
    assert result["unequal_load_decisions"] > 0, "no observed load-dependent choice"
    samples = [json.loads(line) for line in (folder/"routing.metrics.jsonl").read_text().splitlines()]
    peak = max((value for sample in samples for value in metric_values(sample.get("lines",[])).values()),default=0)
    if require_sampled_load:
        assert peak > 0, "no observed native load reservation"
    assert metrics_drained(json.loads((folder/"routing.final.json").read_text())["lines"]), "native load did not drain"
    return dict(status="pass",router_mode="kv",**result,peak_observed_load_gauge=peak,
                sampled_positive_load=peak>0,
                final_prefill_tokens_and_decode_blocks_zero=True,
                lifecycle="Native stream guard marks prefill on token output and frees booking on completion; no simulated replacement.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    print(json.dumps(check(parser.parse_args().folder),indent=2))
