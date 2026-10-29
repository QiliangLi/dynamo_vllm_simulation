"""Independent Fraction worksheet for SA compute and aggregate flow accounting."""
from fractions import Fraction as F
import json
import math
from pathlib import Path


def manual_layer(shapes,p):
    n=sum(u for h,u in shapes)
    pairs=sum(u*h+u*(u+1)//2 for h,u in shapes)
    eta=min(F(1),max(F(p["eta_floor"]),F(n,p["N_sat"])))
    return F(p["t_launch_s"])+F(p["a_s_per_token"])*n/eta+F(p["b_s_per_pair"])*pairs


def check(paths,config):
    p=config["compute"]["profile"];rows=[]
    for path in paths:
        for line in Path(path).read_text().splitlines():
            step=json.loads(line);t=step.get("timing") or {}
            if t.get("mode")!="sa_e26f":continue
            layer=float(manual_layer(t["shapes"],p));total=p["L"]*layer
            error=abs(t["duration_s"]-total)
            assert error<=1e-12 and abs(t["layer_compute_s"]-layer)<=1e-12
            assert sum(step["num_scheduled_tokens"].values())==sum(u for h,u in t["shapes"])
            assert t["real_prefill_tokens"]==t["padded_prefill_tokens"]==sum(u for h,u in t["shapes"])
            rows.append(dict(path=str(path),step=step["step"],program_s=t["duration_s"],
                manual_s=total,error_s=error,bs=len(t["shapes"]),shapes=t["shapes"]))
    assert rows,"no SA executed steps"
    return dict(status="pass",steps=len(rows),rows=rows,max_abs_error_s=max(r["error_s"] for r in rows))


def check_storage(storage,config):
    flows=storage["sa_flow_history"];loads=storage["layer_loads"]
    B=config["storage"]["shared_link_Bps"];qmax=config["storage"]["q_max_Bps"]
    keys=set();members=set()
    for f in flows:
        key=(f["worker"],f["step"],f["layer"]);assert key not in keys;keys.add(key)
        assert f["bytes"]==sum(loads[r]["layer_bytes"] for r in f["rids"])
        for rid in f["rids"]:
            pair=(rid,f["layer"]);assert pair not in members;members.add(pair)
        q=qmax if f["layer"]==0 else min(qmax,f["bytes"]/f["compute_s"])
        assert math.isclose(f["initial_q_Bps"],q,rel_tol=1e-12)
        assert f["completed_s"]-f["submitted_s"]+1e-9>=f["bytes"]/B
    assert len(members)==len(loads)*config["compute"]["profile"]["L"]
    integral=math.fsum((e["t"]-e["from"])*e["actual_Bps"] for e in storage["storage_intervals"])
    assert all(e["actual_Bps"]<=B+B*1e-12 and e["from"]<=e["t"] for e in storage["storage_intervals"])
    assert abs(integral-storage["bytes_transferred"])<1
    assert sum(f["bytes"] for f in flows)==storage["bytes_submitted"]
    # Compare the claimed peak with a sweep over actual batch/layer lifetimes.
    changes={}
    for f in flows:
        changes[f["submitted_s"]]=changes.get(f["submitted_s"],0)+1
        changes[f["completed_s"]]=changes.get(f["completed_s"],0)-1
    active=peak=0
    for t,delta in sorted(changes.items()):
        active+=delta;peak=max(peak,active);assert active>=0
    assert active==0 and peak==storage["peak_pending_requests"]
    return dict(status="pass",batch_layer_flows=len(flows),request_layer_reads=len(members),
        integrated_bytes=integral,byte_error=integral-storage["bytes_transferred"],peak_flows=peak)
