#!/usr/bin/env python3
"""Small real-stack and storage-only time-dilation study, with frozen evidence.

This is not a rerun of the 360-request experiment. Model inputs/rates/profiles
are fixed; only wall_time_scale and admission.mode vary across stack cases.
"""
import argparse
from collections import defaultdict
from copy import deepcopy
import csv
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))
from sim.stack.clock import TimeScale
from diagnose_storage_submit import rpc_trial, summarize, peak_overlap
from run_tfs_stack import run_one


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")


def stack_metrics(folder,check):
    report=json.loads((folder/"report.json").read_text())
    status=json.loads((folder/"admission.final.json").read_text())
    storage=json.loads((folder/"storage.final.json").read_text())
    requests=list(status["records"].values())
    batch=check["actual_batch_sizes"]
    rows=list(report["requests"].values())
    arrival_errors=[r["actual_arrival_s"]-r["planned_arrival_s"] for r in rows]
    history=storage.get("layer_read_history",storage["read_history"])
    assert peak_overlap(history)==storage["peak_pending_requests"]
    cfg=json.loads((folder/"config.json").read_text())
    factor=report["wall_time_scale"]
    sleeps=[]
    for path in sorted(folder.glob("worker*.steps.jsonl")):
        for line in path.read_text().splitlines():
            step=json.loads(line)
            if not step.get("timing"):continue
            duration=step["timing"]["duration_s"]
            assert step["wall_time_scale"]==factor
            assert math.isclose(step["sleep_requested_wall_s"],duration*factor,rel_tol=1e-12,abs_tol=1e-12)
            assert step["sleep_elapsed_s"]+1e-6>=duration*factor
            assert math.isclose(step["sleep_elapsed_model_s"],step["sleep_elapsed_s"]/factor,rel_tol=1e-12)
            sleeps.append(dict(model_s=duration,requested_wall_s=duration*factor,actual_wall_s=step["sleep_elapsed_s"]))
    for event in status["events"]:
        if event["event"]=="feedback_received":
            assert event["visible_s"]+1e-12>=event["occurred_s"]+cfg["admission"]["feedback_delay_s"]
    signatures={rid:{k:r["profile"][k] for k in ("prompt_tokens","estimated_remote_tokens",
        "estimated_read_bytes","standalone_compute_s","pressure_Bps","reference_service_s")}
        for rid,r in status["records"].items()}
    write(folder/"clock_check.json",dict(status="pass",wall_time_scale=factor,sleeps=sleeps,
        profile_sha256=hashlib.sha256(json.dumps(signatures,sort_keys=True).encode()).hexdigest()))
    caps=[e["cap"] for e in status["events"] if e["event"]=="threshold_updated" and e.get("cap") is not None]
    return dict(ttft_mean_ms=report["ttft_mean_s"]*1000,
        ttft_wall_mean_ms=report["ttft_mean_wall_s"]*1000,
        ttft_max_ms=report["ttft_max_s"]*1000,
        model_window_ms=report["model_elapsed_s"]*1000,
        compute_model_ms=check["model_sum_s"]*1000,
        central_wait_ms=check["central_wait_mean_s"]*1000,
        slo_fraction=sum(r["first_token_s"]<=r["profile"]["deadline_s"] for r in requests)/len(requests),
        peak_readers=storage["peak_pending_requests"],
        multi_request_step_fraction=sum(b>1 for b in batch)/len(batch),
        batch_mean=statistics.mean(batch),
        held_heavy_peak=check["held_heavy_peak"],
        minimum_quota_cap=min(caps) if caps else 0,
        quota_can_bind=int(bool(caps) and min(caps)<=len(requests)),
        aging_exemptions=sum(e["event"]=="aging_exempted" for e in status["events"]),
        arrival_error_max_ms=max(arrival_errors)*1000,
        read_bytes=storage["bytes_submitted"],requests=report["n_ok"])


def analyze(output,rows,tolerance):
    grouped=defaultdict(list)
    for row in rows:
        if row["status"]=="pass":
            grouped[(row["phase"],row["mode"],row["scale"])].append(row)
    summaries=[]
    for (phase,mode,scale),runs in sorted(grouped.items()):
        metrics={key:statistics.median(r["metrics"][key] for r in runs)
                 for key in runs[0]["metrics"]}
        summaries.append(dict(phase=phase,mode=mode,scale=scale,repeats=len(runs),metrics=metrics,
                              peaks=[r["metrics"]["peak_readers"] for r in runs]))
    comparisons=[]
    for phase,mode in sorted({(r["phase"],r["mode"]) for r in summaries}):
        cases=sorted((r for r in summaries if (r["phase"],r["mode"])==(phase,mode)),key=lambda r:r["scale"])
        if len(cases)<2:
            continue
        a,b=cases[-2:]
        keys=("ttft_mean_ms","compute_model_ms") if phase=="stack" else ("read_span_model_ms","read_median_model_ms")
        changes={key:abs(a["metrics"][key]-b["metrics"][key])/max(abs(b["metrics"][key]),1e-12) for key in keys}
        comparisons.append(dict(phase=phase,mode=mode,scales=[a["scale"],b["scale"]],
            relative_change=changes,tolerance=tolerance,
            time_metrics_stable=all(v<=tolerance for v in changes.values()),
            peaks=[a["peaks"],b["peaks"]],
            boundary="A local screening rule on median timing metrics; not a proof of convergence, "
                     "production calibration or a TFS benefit estimate."))
    result=dict(analyzer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        cases=summaries,highest_scale_comparisons=comparisons,
        trials=len(rows),failed=sum(r["status"]!="pass" for r in rows),
        aggregation="Median across runs for each metric; repeats and each run's peak are retained.",
        limitations=["Small synthetic workload, not Mooncake360.",
            "Real Dynamo/vLLM control code still runs in wall time; OS, HTTP, polling and batching may remain scale-sensitive.",
            "Storage-only and stack phases measure different workloads; do not combine their latencies."])
    write(output/"analysis.json",result)
    (output/"analysis_sources").mkdir(exist_ok=True)
    (output/"analysis_sources"/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    with (output/"summary.csv").open("w",newline="") as stream:
        columns=["phase","mode","scale","repeats","peaks","metric","median"]
        writer=csv.DictWriter(stream,fieldnames=columns,lineterminator="\n")
        writer.writeheader()
        for row in summaries:
            for metric,value in row["metrics"].items():
                writer.writerow({**{k:row[k] for k in columns[:4]},"peaks":"/".join(map(str,row["peaks"])),
                                 "metric":metric,"median":value})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--trace",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--scales",type=float,nargs="+",default=[1,10,50,100])
    parser.add_argument("--repeats",type=int,default=3)
    parser.add_argument("--workers",type=int,default=2)
    parser.add_argument("--phase",choices=["storage","stack","all"],default="all")
    parser.add_argument("--tolerance",type=float,default=.05)
    args=parser.parse_args()
    if len(set(args.scales))!=len(args.scales) or args.repeats<1 or args.workers<1:
        parser.error("invalid dimensions")
    for scale in args.scales:TimeScale(scale)
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    config=json.loads(args.config.read_text())
    write(output/"config.json",config)
    (output/"trace.jsonl").write_bytes(args.trace.read_bytes())
    paths=[*sorted((ROOT/"sim").glob("*.py")),*sorted((ROOT/"sim/stack").glob("*.py")),
           Path(__file__),ROOT/"scripts/replay_http.py",ROOT/"scripts/run_tfs_stack.py",
           ROOT/"scripts/diagnose_storage_submit.py",ROOT/"scripts/stack_vllm_worker.py",
           ROOT/"scripts/check_prefill_formula.py"]
    for path in paths:
        dest=output/"runtime_sources"/path.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(path.read_bytes())
    metadata=dict(platform=platform.platform(),python=sys.version,scales=args.scales,
        repeats=args.repeats,workers=args.workers,tolerance=args.tolerance,phase=args.phase,
        created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        packages={d.metadata["Name"]:d.version for d in importlib.metadata.distributions()},
        trace_sha256=hashlib.sha256(args.trace.read_bytes()).hexdigest(),trials=[])
    write(output/"report.json",metadata)
    rows=[]
    with (output/"events.jsonl").open("w") as events:
        try:
            for phase in (["storage","stack"] if args.phase=="all" else [args.phase]):
                for repeat in range(args.repeats):
                    scales=args.scales if repeat%2==0 else args.scales[::-1]
                    for scale in scales:
                        modes=["rpc"] if phase=="storage" else (["immediate","tfs"] if repeat%2==0 else ["tfs","immediate"])
                        for mode in modes:
                            name=f"{phase}_{mode}_k{scale:g}_r{repeat}"
                            entry=dict(phase=phase,mode=mode,scale=scale,repeat=repeat,case=name)
                            cfg=deepcopy(config);cfg["wall_time_scale"]=scale
                            print(f"starting {name}",flush=True)
                            try:
                                if phase=="storage":
                                    trial=rpc_trial(cfg,8,512,0,True,False)
                                    expected=8*512*cfg["storage"]["kv_bytes_per_token"]
                                    assert trial["pending"]==0 and abs(trial["bytes_transferred"]-expected)<1
                                    assert peak_overlap(trial["reads"])==trial["peak_pending"]
                                    metrics=summarize(trial)
                                    span=max(r["completed_s"] for r in trial["reads"])-min(r["submitted_s"] for r in trial["reads"])
                                    entry["metrics"]=dict(peak_readers=trial["peak_pending"],read_span_model_ms=span*1000,
                                        read_median_model_ms=metrics["model_read"]["median_ms"],
                                        submit_call_model_ms=metrics["submit_call"]["median_ms"]/scale,
                                        submit_operation_wall_ms=metrics["operation"]["median_ms"])
                                    entry["trial"]=trial
                                else:
                                    check=run_one(cfg,output/"trace.jsonl",output/name,
                                        Path(sys.executable),mode,"first_token",args.workers)
                                    entry["metrics"]=stack_metrics(output/name,check)
                                entry["status"]="pass"
                            except BaseException:
                                entry.update(status="failed",error=traceback.format_exc())
                                raise
                            finally:
                                rows.append(entry);events.write(json.dumps(entry)+"\n");events.flush()
                                metadata["trials"]=[{k:v for k,v in r.items() if k!="trial"} for r in rows]
                                write(output/"report.json",metadata)
                            print(json.dumps({k:v for k,v in entry.items() if k!="trial"}),flush=True)
        finally:
            result=analyze(output,rows,args.tolerance)
            print(json.dumps(result["highest_scale_comparisons"]),flush=True)


if __name__=="__main__":main()
