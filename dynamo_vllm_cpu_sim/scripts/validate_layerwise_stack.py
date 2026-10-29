"""Functional real-stack matrix: fixed synthetic input, three KV modes, both admission arms.

This validates execution semantics. It is not an estimate of TFS performance.
Run with PYTHONHASHSEED=0, like the worker/storage processes.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"scripts")]
from run_tfs_stack import run_one,write


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--time-scale",type=float,default=100)
    args=p.parse_args()
    if os.environ.get("PYTHONHASHSEED")!="0":
        p.error("set PYTHONHASHSEED=0 for deterministic cross-process block hashes")
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    cfg=json.loads((ROOT/"configs/mooncake_first_seen_layerwise.json").read_text())
    cfg.update(max_model_len=2048,num_blocks=8193,max_num_batched_tokens=64,
               wall_time_scale=args.time_scale)
    cfg["admission"]["feedback_delay_s"]=.001
    # Safe GPT-2 token roundtrips; all rows arrive together to exercise queued
    # native batches. Remote h=32/512, zero-read h=0; u=97/129 requires chunks.
    rng=random.Random(418);rows=[]
    for i,kind in enumerate("ABACABCA"):
        h,u={"A":(512,1),"B":(32,97),"C":(0,129)}[kind]
        rows.append(dict(id=f"layer{i:02}",arrival_s=0.,
            prompt_token_ids=[rng.choice([1332,995,2581,640,1366,23748]) for _ in range(h+u)],
            output_tokens=1,remote_prefix_tokens=h,hit_tokens=h,u_tokens=u,
            full_hit_adjusted=(u==1),cache_model="first_seen",class_id=kind))
    trace=out/"trace.jsonl"
    trace.write_text("".join(json.dumps(r)+"\n" for r in rows))
    write(out/"config.json",cfg)
    paths=[*sorted((ROOT/"sim").rglob("*.py")),Path(__file__),
           ROOT/"scripts/run_tfs_stack.py",ROOT/"scripts/replay_http.py",
           ROOT/"scripts/check_layerwise.py",ROOT/"scripts/check_prefill_formula.py",
           ROOT/"scripts/stack_vllm_worker.py"]
    fingerprints={}
    for path in paths:
        dest=out/"runtime_sources"/path.relative_to(ROOT)
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(path.read_bytes())
        fingerprints[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    write(out/"provenance.json",dict(sources=fingerprints,python=sys.version,
        trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        boundary="8 synthetic first_seen requests, two real workers, chunk budget 64; not Mooncake360."))
    cases=[dict(name=f"{mode}_{kv}",mode=mode,kv=kv,release="first_token",disks=1)
           for mode in ("immediate","tfs") for kv in ("bulk","layer_serial","layer_pipeline")]
    cases += [dict(name="tfs_pipeline_three_disk_load_done",mode="tfs",kv="layer_pipeline",
                   release="load_done",disks=3),
              dict(name="immediate_pipeline_per_layer_startup",mode="immediate",kv="layer_pipeline",
                   release="first_token",disks=1,startup="per_layer")]
    results=[]
    for case in cases:
        c=deepcopy(cfg);c["kv_transfer_mode"]=case["kv"]
        if case["disks"]==3:c["storage"].update(disks=3,disk_Bps=40e9)
        c["layerwise"]["startup"]=case.get("startup","once")
        begin=time.monotonic();row=dict(case=case,config=c)
        try:
            check=run_one(c,trace,out/case["name"],Path(sys.executable),case["mode"],case["release"],2)
            layer_file=out/case["name"]/"layer_check.json"
            layer=json.loads(layer_file.read_text()) if layer_file.exists() else None
            assert check["workers"]==[0,1]
            assert check["actual_submitted_read_bytes"]==231948288
            # Chunked u cannot be inferred from load count; inspect real shapes.
            hits=json.loads((out/case["name"]/"hit_check.json").read_text())
            assert any(len(r["actual_shapes"])>1 for r in hits["requests"].values())
            if layer:
                assert layer["layer_reads"]==468
                assert layer["steps"]>4
                if case["kv"]=="layer_pipeline":assert layer["prefetches_within_compute"]==462
                else:assert layer["prefetches_within_compute"]==0
            row.update(status="pass",check=check,layer_check=layer)
        except BaseException:
            row.update(status="failed",error=traceback.format_exc());raise
        finally:
            row["elapsed_wall_s"]=time.monotonic()-begin
            results.append(row);write(out/"results.json",results)
            with (out/"events.jsonl").open("a") as f:f.write(json.dumps(row)+"\n")
    assert len({r["check"]["actual_submitted_read_bytes"] for r in results})==1
    write(out/"acceptance.json",dict(status="pass",cases=len(results),requests=8*len(results),
        source_sha256=fingerprints,limitations=["Functional synthetic matrix; not a TFS performance comparison.",
            "CPU model and wall-clock execution; no real GPU KV tensors or calibrated target timings."]))


if __name__=="__main__":main()
