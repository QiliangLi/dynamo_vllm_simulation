"""Independently audit completed original-workload runs and read overlaps."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values,q):
    ordered=sorted(values)
    index=(len(ordered)-1)*q
    low=int(index)
    return ordered[low]+(ordered[min(low+1,len(ordered)-1)]-ordered[low])*(index-low)


def overlap(reads):
    changes=Counter()
    for r in reads:
        assert r["completed_s"] >= r["submitted_s"]
        changes[r["submitted_s"]]+=1
        changes[r["completed_s"]]-=1
    active=peak=0
    previous=min(changes)
    durations=Counter()
    for instant,delta in sorted(changes.items()):
        durations[active]+=instant-previous
        active+=delta;peak=max(active,peak);previous=instant
        assert active>=0
    assert active==0
    span=max(changes)-min(changes)
    return dict(peak=peak,span_model_s=span,
        duration_by_concurrency_model_s=dict(sorted(durations.items())),
        multiple_fraction_of_span=sum(t for n,t in durations.items() if n>1)/span,
        mean_concurrency=sum(n*t for n,t in durations.items())/span)


def audit(root):
    entries=[];implementation=set();trace=set();profiles=set();configs=set()
    original=[json.loads(s) for s in (root/"trace.jsonl").read_text().splitlines()]
    assert len(original)==360
    for path in sorted(root.glob("*.result.json")):
        entry=json.loads(path.read_text())
        if entry["status"]!="pass":
            entries.append(dict(case=entry["case"],status=entry["status"],error=entry.get("error")))
            continue
        name=entry["case"];folder=root/name
        provenance=json.loads((root/(name+".provenance.json")).read_text())
        for relative,digest in provenance["sources"].items():
            assert sha(root/"runtime_sources"/relative)==digest
        for relative,digest in provenance["adapters"].items():
            assert sha(root/relative)==digest
        assert not entry["guards"] and not entry["alive_pids"]
        cfg=json.loads((folder/"config.json").read_text())
        k=cfg.pop("wall_time_scale",1.)
        mode=cfg["admission"].pop("mode")
        assert k==entry["scale"] and mode==entry["mode"]
        assert cfg["workers"]==8 and cfg["block_size"]==1 and cfg["max_num_seqs"]==4
        assert cfg["storage"]["disks"]==1 and cfg["storage"]["disk_Bps"]==120e9
        configs.add(json.dumps(cfg,sort_keys=True))
        implementation.add(json.dumps(json.loads((folder/"implementation.json").read_text()),sort_keys=True))
        check=json.loads((folder/"check.json").read_text())
        ready=json.loads((folder/"readiness.json").read_text())
        assert ready["expected_workers"]==8
        assert ready["frontend"]["namespaces"]["dynamo"]["worker_types"]["aggregated"]["workers"]==8
        assert [json.loads(s) for s in (folder/"trace.jsonl").read_text().splitlines()]==original
        report=json.loads((folder/"report.json").read_text())
        storage=json.loads((folder/"storage.final.json").read_text())
        state=json.loads((folder/"admission.final.json").read_text())
        clock=json.loads((folder/"clock_check.json").read_text())
        assert clock["status"]==check["status"]=="pass"
        assert report["n_ok"]==report["n_total"]==entry["requests"]==360
        assert check["workers"]==list(range(8))
        assert storage["bytes_submitted"]==65257530624
        assert abs(storage["bytes_transferred"]-65257530624)<1
        assert storage["pending_reads"]==0 and len(storage["read_history"])==350
        assert json.loads((folder/"formula_check.json").read_text())["status"]=="pass"
        assert json.loads((folder/"hit_check.json").read_text())["status"]=="pass"
        trace.add(sha(folder/"trace.jsonl"));profiles.add(clock["profile_sha256"])
        occupancy=overlap(storage["read_history"])
        assert occupancy["peak"]==storage["peak_pending_requests"]==entry["metrics"]["peak_readers"]
        records=list(state["records"].values())
        worker_counts=Counter(r["worker_id"] for r in records)
        assert worker_counts==Counter({w:45 for w in range(8)}),worker_counts
        caps=[e for e in state["events"] if e["event"]=="admission_reserved" and e["heavy"]]
        quota_hit=any(e["held_after"]>=e["cap"] for e in caps)
        batch=Counter(check["actual_batch_sizes"])
        read_ms=[(r["completed_s"]-r["submitted_s"])*1000 for r in storage["read_history"]]
        ttft_ms=[r["ttft_s"]*1000 for r in report["requests"].values()]
        distribution=dict(read_latency_mean_ms=statistics.mean(read_ms),
            read_latency_p95_ms=percentile(read_ms,.95),read_latency_max_ms=max(read_ms),
            ttft_p95_ms=percentile(ttft_ms,.95))
        entries.append(dict(case=name,status="pass",mode=mode,scale=k,metrics=entry["metrics"],
            occupancy=occupancy,distribution=distribution,batch_histogram=dict(batch),quota_reached=quota_hit,
            worker_request_counts=dict(sorted(worker_counts.items())),
            maximum_held_cap_ratio=max((e["held_after"]/e["cap"] for e in caps),default=0),
            gateway_ttft_model_s=statistics.mean(r["first_token_s"]-r["profile"]["arrival_s"] for r in records),
            wall_elapsed_s=entry["wall_elapsed_s"],source_result_sha256=sha(path)))
    for values in (implementation,trace,profiles,configs):assert len(values)<=1
    full=[r for r in entries if r["status"]=="pass"]
    summary=[]
    for mode,k in sorted({(r["mode"],r["scale"]) for r in full}):
        rows=[r for r in full if (r["mode"],r["scale"])==(mode,k)]
        summary.append(dict(mode=mode,scale=k,repeats=len(rows),peaks=[r["occupancy"]["peak"] for r in rows],
            metrics={key:statistics.median(r["metrics"][key] for r in rows) for key in rows[0]["metrics"]},
            distribution={key:statistics.median(r["distribution"][key] for r in rows) for key in rows[0]["distribution"]}))
    comparisons=[]
    for mode in sorted({r["mode"] for r in summary}):
        rows=sorted((r for r in summary if r["mode"]==mode),key=lambda r:r["scale"])
        for a,b in zip(rows,rows[1:]):
            keys=("ttft_mean_ms","compute_model_ms","read_latency_median_model_ms")
            changes={key:abs(a["metrics"][key]-b["metrics"][key])/max(abs(b["metrics"][key]),1e-12) for key in keys}
            distribution_changes={key:abs(a["distribution"][key]-b["distribution"][key])/max(abs(b["distribution"][key]),1e-12)
                                  for key in ("read_latency_mean_ms","read_latency_p95_ms","ttft_p95_ms")}
            comparisons.append(dict(mode=mode,scales=[a["scale"],b["scale"]],relative_changes=changes,
                within_five_percent=all(v<=.05 for v in changes.values()),
                supplementary_distribution_relative_changes=distribution_changes,
                supplementary_distributions_within_five_percent=all(v<=.05 for v in distribution_changes.values()),
                boundary="Local timing screen only; no claim of global convergence or TFS benefit."))
    result=dict(audit_source_sha256=sha(Path(__file__)),cases=entries,summary=summary,scale_comparisons=comparisons,
        full_passed=len(full),failed=len(entries)-len(full),successful_requests=360*len(full),
        invariant_implementation=len(implementation)==1,invariant_trace=len(trace)==1,
        invariant_profiles=len(profiles)==1,invariant_config_except_mode_and_scale=len(configs)==1,
        boundary="Old six runs used a different OS/host; cross-host TTFT differences do not identify repair speedup.")
    (root/"audit.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    target=root/"analysis_sources";target.mkdir(exist_ok=True)
    (target/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps({k:v for k,v in result.items() if k!="cases"},ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    audit(parser.parse_args().root)
