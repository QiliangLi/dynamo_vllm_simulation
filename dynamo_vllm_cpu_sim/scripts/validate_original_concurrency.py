"""Run the archived Mooncake360 workload on eight real engines, with evidence.

Use the archived experiment's adapters on both arms. Only the HTTP timeout is
scaled with the model clock. A prefix run is a preflight, never a full acceptance.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import threading
import time
import traceback

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from run_tfs_stack import run_one, write
from validate_time_scaling import stack_metrics
import replay_http
import httpx


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    output = args.output.resolve()
    cfg = json.loads((output / "config.json").read_text())
    cfg["wall_time_scale"] = args.scale
    if args.kv_mode is not None:
        cfg["kv_transfer_mode"] = args.kv_mode
    trace = output / "trace.jsonl"
    rows = [json.loads(s) for s in trace.read_text().splitlines()]
    assert len(rows) == 360 and sum(r["hit_tokens"] * cfg["storage"]["kv_bytes_per_token"] for r in rows) == 65257530624
    assert cfg["workers"] == 8 and cfg["storage"]["disk_Bps"] == 120e9 and cfg["storage"]["disks"] == 1
    if args.prefix:
        rows = rows[:args.prefix]
        trace = output / f"prefix{args.prefix}.jsonl"
        trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
    name = args.name
    assert not (output / name).exists()
    source = output / "runtime_sources"
    paths = [*sorted((ROOT / "sim").rglob("*.py")), Path(__file__),
             ROOT / "scripts/run_tfs_stack.py", ROOT / "scripts/replay_http.py",
             ROOT / "scripts/validate_time_scaling.py", ROOT / "scripts/check_prefill_formula.py",
             ROOT / "scripts/check_layerwise.py",
             ROOT / "scripts/stack_vllm_worker.py"]
    fingerprints = {}
    for path in paths:
        relative = path.relative_to(ROOT)
        dest = source / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            assert dest.read_bytes() == path.read_bytes(), f"implementation changed: {relative}"
        else:
            dest.write_bytes(path.read_bytes())
        fingerprints[str(relative)] = digest(path)
    write(output / (name + ".provenance.json"), dict(
        trace_sha256=digest(trace), original_trace_sha256=digest(output / "trace.jsonl"),
        config_sha256=digest(output / "config.json"), sources=fingerprints,
        adapters={p.name:digest(p) for p in (output/"gateway.py", output/"storage_transport.py")},
        scale=args.scale, mode=args.mode, kv_transfer_mode=cfg.get("kv_transfer_mode","bulk"), requests=len(rows), workers=8,
        platform=sys.platform, python=sys.version, wall_budget_s=args.wall_budget,
        min_available_mib=args.min_available_mib,
        adapter_boundary="Archived tokenizer/profile/reference-bind/backlog/HTTP pool adapters; only HTTP timeout multiplied by K."))

    original_popen, original_client = subprocess.Popen, httpx.AsyncClient
    def launch(argv, *a, **kw):
        if isinstance(argv,list) and argv[1:3] == ["-m", "sim.stack.admission_service"]:
            argv = [argv[0], str(output/"gateway.py"), *argv[3:]]
        elif isinstance(argv,list) and argv[1:3] == ["-m", "sim.stack.storage_service"]:
            argv = [argv[0], str(output/"storage_transport.py"), *argv[3:]]
        return original_popen(argv,*a,**kw)

    class ExperimentClient(original_client):
        def __init__(self,*a,**kw):
            kw.update(timeout=600*args.scale, limits=httpx.Limits(max_connections=512,max_keepalive_connections=64))
            super().__init__(*a,**kw)

    subprocess.Popen, httpx.AsyncClient = launch, ExperimentClient
    parent = psutil.Process()
    stopped = threading.Event()
    captured, samples, guard = {}, [], []
    phase = ["startup"]
    started = time.monotonic()
    original_replay = replay_http.replay
    async def measured(*a,**kw):
        phase[0] = "replay"
        write(output / "progress.json", dict(case=name,phase=phase[0],scale=args.scale))
        try:
            return await original_replay(*a,**kw)
        finally:
            phase[0] = "verification"
    replay_http.replay = measured

    def monitor():
        low_since = None
        with (output/(name+".resources.jsonl")).open("w",buffering=1) as log:
            while not stopped.is_set():
                children = []
                for process in parent.children(recursive=True):
                    try:
                        item=dict(pid=process.pid,created=process.create_time(),rss=process.memory_info().rss)
                        captured[(item["pid"],item["created"])]=item
                        children.append(item)
                    except psutil.Error:
                        pass
                sample=dict(elapsed_s=time.monotonic()-started,phase=phase[0],processes=children,
                    available_bytes=psutil.virtual_memory().available,swap_used=psutil.swap_memory().used)
                samples.append(sample);log.write(json.dumps(sample)+"\n")
                low = sample["available_bytes"] < args.min_available_mib*2**20
                low_since = (low_since or time.monotonic()) if low else None
                reason = ("memory" if low_since and time.monotonic()-low_since>10 else
                          "wall_budget" if sample["elapsed_s"]>args.wall_budget else None)
                if reason and not guard:
                    guard.append(dict(reason=reason,sample=sample))
                    os.kill(os.getpid(),signal.SIGINT)
                stopped.wait(1)
    thread = threading.Thread(target=monitor,daemon=True)
    thread.start()
    write(output/"progress.json",dict(case=name,phase="startup",scale=args.scale))
    entry = dict(case=name,mode=args.mode,scale=args.scale,requests=len(rows),full_original=len(rows)==360,
                 kv_transfer_mode=cfg.get("kv_transfer_mode","bulk"))
    try:
        check = run_one(cfg, trace, output/name, Path(sys.executable), args.mode,"first_token",8)
        metrics = stack_metrics(output/name,check)
        storage=json.loads((output/name/"storage.final.json").read_text())
        state=json.loads((output/name/"admission.final.json").read_text())
        history=storage.get("layer_read_history",storage["read_history"])
        arrivals=[r["profile"]["arrival_s"] for r in state["records"].values()]
        history=sorted(history,key=lambda r:r["submitted_s"])
        gaps=[b["submitted_s"]-a["submitted_s"] for a,b in zip(history,history[1:])]
        metrics.update(reads=len(history), gateway_arrival_span_model_ms=(max(arrivals)-min(arrivals))*1000,
            read_latency_median_model_ms=statistics.median(r["completed_s"]-r["submitted_s"] for r in history)*1000,
            submit_gap_median_model_ms=statistics.median(gaps)*1000,
            read_span_model_ms=(max(r["completed_s"] for r in history)-min(r["submitted_s"] for r in history))*1000)
        assert check["workers"]==list(range(8)),check
        assert check["actual_submitted_read_bytes"]==sum(r["hit_tokens"]*cfg["storage"]["kv_bytes_per_token"] for r in rows)
        entry.update(status="pass",metrics=metrics)
    except BaseException:
        entry.update(status="failed",error=traceback.format_exc())
        raise
    finally:
        stopped.set();thread.join(10)
        replay_http.replay=original_replay
        subprocess.Popen,httpx.AsyncClient=original_popen,original_client
        alive=[]
        for pid,created in captured:
            try:
                p=psutil.Process(pid)
                if p.create_time()==created and p.status()!=psutil.STATUS_ZOMBIE:alive.append(pid)
            except psutil.Error:pass
        entry.update(wall_elapsed_s=time.monotonic()-started,guards=guard,alive_pids=alive,
            min_available_gib=min(s["available_bytes"] for s in samples)/2**30,
            max_swap_gib=max(s["swap_used"] for s in samples)/2**30,
            peak_children_rss_gib=max(sum(p["rss"] for p in s["processes"]) for s in samples)/2**30)
        if guard or alive:entry["status"]="failed"
        write(output/(name+".result.json"),entry)
        with (output/"events.jsonl").open("a") as stream:stream.write(json.dumps(entry)+"\n")
        write(output/"progress.json",entry)
        print(json.dumps(entry),flush=True)
    assert not guard and not alive


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--name",required=True)
    parser.add_argument("--mode",choices=["immediate","tfs"],required=True)
    parser.add_argument("--scale",type=float,required=True)
    parser.add_argument("--prefix",type=int)
    parser.add_argument("--kv-mode",choices=["bulk","layer_serial","layer_pipeline"])
    parser.add_argument("--wall-budget",type=float,default=3600)
    parser.add_argument("--min-available-mib",type=float,default=768)
    run(parser.parse_args())
