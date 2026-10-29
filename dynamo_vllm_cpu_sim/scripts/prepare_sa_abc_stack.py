"""Freeze the original E26f A/B/C trace at an exact finite-window storage demand.

Only arrival times and their derived deadlines change. h/u, class counts and
SA reference times are preserved. Synthetic text is roundtrip-checked before
any real-stack experiment; it is not a new cache-hit workload.
"""
import argparse
from collections import Counter
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sim.sa_model import reference


def prepare(source,config,output,rho=.8):
    rows=json.loads(source.read_text());cfg=json.loads(config.read_text())
    if not 0<rho<=1:raise ValueError("rho must be in (0,1]")
    assert Counter(r["class_id"] for r in rows)==Counter(A=120,B=120,C=120)
    p=cfg["compute"]["profile"];B=cfg["storage"]["shared_link_Bps"]
    total=sum(r["h_tokens"]*cfg["storage"]["kv_bytes_per_token"] for r in rows)
    duration=float(F(total)/(F(str(rho))*F(str(B))))
    oldend=max(r["arrival_s"] for r in rows);scale=duration/oldend
    from transformers import AutoTokenizer
    tok=AutoTokenizer.from_pretrained(str(ROOT/"configs/model"))
    templates={};trace=[];checks=[]
    motif=[1332,995,2581,640,1366,23748]
    for r in rows:
        h,u=r["h_tokens"],r["u_tokens"]
        v,c,t0=reference(r,p,B,cfg["storage"]["q_max_Bps"])
        assert abs(t0-r["T0_s"])<1e-10
        shape=(h,u)
        if shape not in templates:
            tokens=(motif*((h+u+len(motif)-1)//len(motif)))[:h+u]
            text=tok.decode(tokens);assert tok.encode(text)==tokens
            templates[shape]=tokens
            checks.append(dict(h=h,u=u,prompt_tokens=h+u,roundtrip_passed=True,
                standalone_compute_s=c*p["L"],T0_s=t0))
        arrival=r["arrival_s"]*scale
        trace.append(dict(id=r["id"],class_id=r["class_id"],arrival_s=arrival,
            prompt_token_ids=templates[shape],remote_prefix_tokens=h,hit_tokens=h,u_tokens=u,
            output_tokens=1,full_hit_adjusted=False,cache_model="first_seen",
            sa_rid=r["sa_rid"],T0_s=r["T0_s"],deadline_s=arrival+4*r["T0_s"]))
    assert abs(max(r["arrival_s"] for r in trace)-duration)<1e-12
    output.mkdir(parents=True,exist_ok=False)
    (output/"trace.jsonl").write_text("".join(json.dumps(r,separators=(",",":"))+"\n" for r in trace))
    (output/"config.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n")
    (output/"source.json").write_bytes(source.read_bytes())
    metadata=dict(source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        trace_sha256=hashlib.sha256((output/"trace.jsonl").read_bytes()).hexdigest(),
        seed=416,variant="C_A",classes=dict(Counter(r["class_id"] for r in rows)),
        storage_target_input_load=rho,storage_Bps=B,total_read_bytes=total,
        planned_injection_window_s=duration,arrival_scale=scale,original_last_arrival_s=oldend,
        global_window_mean_req_s=len(rows)/duration,workers=cfg["workers"],
        profile_checks=checks,
        timing="Finite-window normalization from origin 0 to last planned arrival; excludes drain.",
        stochastic_boundary="Original A-cluster/B-C-Poisson sample rescaled as a whole; not an unconditioned Poisson trial.",
        token_boundary="Three safe synthetic text templates, local prefix cache off; frozen per-request h overrides physical cross-request presence.")
    (output/"input_check.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\n")
    return metadata


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",type=Path,default=ROOT/"results/sa_alignment_20261019/source/D1fA-s416.json")
    p.add_argument("--config",type=Path,default=ROOT/"configs/sa_abc_stack.json")
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--rho",type=float,default=.8)
    a=p.parse_args();print(json.dumps(prepare(a.source,a.config,a.output,a.rho),ensure_ascii=False))
