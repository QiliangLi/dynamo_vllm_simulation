#!/usr/bin/env python3
"""Audit and combine saved scaling runs without rerunning any simulation."""
import argparse
import hashlib
import json
from pathlib import Path

from validate_time_scaling import analyze


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def audit(inputs,output,tolerance):
    rows=[];seen=set();sources=[];implementations=set();profiles=set();traces=set();configs=set();read_bytes=set()
    core_sources=[];stack_requests=0;storage_reads=0
    for directory in inputs:
        report=json.loads((directory/"report.json").read_text())
        raw=[json.loads(line) for line in (directory/"events.jsonl").read_text().splitlines()]
        assert len(raw)==len(report["trials"])
        core_sources.append({k:v for k,v in report["source_sha256"].items() if k!="scripts/validate_time_scaling.py"})
        for relative,digest in report["source_sha256"].items():
            assert sha(directory/"runtime_sources"/relative)==digest
        for entry,summary in zip(raw,report["trials"]):
            assert {k:v for k,v in entry.items() if k!="trial"}==summary
            key=entry["phase"],entry["mode"],entry["scale"],entry["repeat"]
            assert key not in seen and entry["status"]=="pass"
            seen.add(key)
            if entry["phase"]=="stack":
                case=directory/entry["case"]
                cfg=json.loads((case/"config.json").read_text())
                scale=cfg.pop("wall_time_scale",1.)
                cfg["admission"].pop("mode")
                configs.add(canonical(cfg))
                implementation=json.loads((case/"implementation.json").read_text())
                assert implementation["sim/stack/clock.py"]==report["source_sha256"]["sim/stack/clock.py"]
                implementations.add(canonical(implementation))
                clock=json.loads((case/"clock_check.json").read_text())
                assert clock["status"]=="pass" and clock["wall_time_scale"]==scale==entry["scale"]
                profiles.add(clock["profile_sha256"])
                traces.add(sha(case/"trace.jsonl"))
                assert traces=={report["trace_sha256"]}
                storage=json.loads((case/"storage.final.json").read_text())
                assert storage["pending_reads"]==0
                assert abs(storage["bytes_transferred"]-storage["bytes_submitted"])<1
                read_bytes.add(storage["bytes_submitted"])
                stack_requests+=entry["metrics"]["requests"]
                assert json.loads((case/"formula_check.json").read_text())["status"]=="pass"
                if cfg.get("cache_model")=="first_seen":
                    assert json.loads((case/"hit_check.json").read_text())["status"]=="pass"
            else:
                trial=entry["trial"]
                assert trial["pending"]==0 and abs(trial["bytes_transferred"]-trial["bytes_submitted"])<1
                storage_reads+=len(trial["reads"])
            # Keep raw RPC rows once in each source directory. The combined
            # index carries their source and hashes, rather than duplicating
            # potentially large per-poll traces.
            rows.append(dict({k:v for k,v in entry.items() if k!="trial"},
                             source_directory=directory.name))
        sources.append(dict(directory=directory.name,report_sha256=sha(directory/"report.json"),
                            events_sha256=sha(directory/"events.jsonl")))
    assert all(source==core_sources[0] for source in core_sources)
    for values in (implementations,profiles,traces,configs,read_bytes):assert len(values)<=1
    output.mkdir(parents=True,exist_ok=False)
    (output/"events.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    result=analyze(output,rows,tolerance)
    audit_result=dict(status="pass",trials=len(rows),stack_requests=stack_requests,
        storage_only_reads=storage_reads,source_reports=sources,unchanged_stack_implementation=len(implementations)==1,
        invariant_profiles=len(profiles)==1,invariant_trace=len(traces)==1,
        invariant_config_except_scale_and_mode=len(configs)==1,stack_read_bytes_per_case=sorted(read_bytes),
        audit_source_sha256=sha(Path(__file__)),analyzer_sha256=result["analyzer_sha256"],
        boundary="Initial and extended executions retain their own frozen source. Only analysis was revised "
                 "to include per-request read latency, since total read span alone can follow byte conservation.")
    (output/"report.json").write_text(json.dumps(audit_result,indent=2)+"\n")
    (output/"analysis_sources"/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps(audit_result,indent=2))
    print(json.dumps(result["highest_scale_comparisons"],indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs",nargs="+",type=Path)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--tolerance",type=float,default=.05)
    args=parser.parse_args()
    audit(args.inputs,args.output,args.tolerance)
