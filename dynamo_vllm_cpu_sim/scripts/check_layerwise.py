"""Independent audit of actual per-layer IO, fixed native batches and barriers."""
from collections import Counter,defaultdict
import hashlib
import json
import math
from pathlib import Path


def check(folder,config,storage):
    from check_prefill_formula import manual_timing
    layers=storage["layer_spec"]["layers"]
    size=storage["layer_spec"]["bytes_per_token"]*config["block_size"]
    k=config.get("wall_time_scale",1.)
    loads=storage["layer_loads"]
    history=storage["layer_read_history"]
    actual={(r["rid"],r["layer"]):r for r in history}
    assert len(actual)==len(history)==len(loads)*layers
    assert sum(r["bytes"] for r in history)==storage["bytes_submitted"]
    completed_mono={}
    for rid,load in loads.items():
        assert load["submitted_layers"]==load["completed_layers"]==list(range(layers))
        assert load["submitted_bytes"]==load["completed_bytes"]==load["total_bytes"]
        assert load["total_bytes"]==len(load["blocks_hex"])*config["block_size"]*config["storage"]["kv_bytes_per_token"]
        expected=Counter(max(range(config["storage"]["disks"]),key=lambda d:
            hashlib.sha256(bytes.fromhex(h)+d.to_bytes(4,"little")).digest()) for h in load["blocks_hex"])
        assert expected==Counter({int(d):n for d,n in load["counts"].items()})
        for layer in range(layers):
            r=actual[(rid,layer)]
            counts={int(d):n for d,n in load["counts"].items()}
            assert r["disk_bytes"]==[counts.get(d,0)*size for d in range(config["storage"]["disks"])]
            assert r["bytes"]==sum(r["disk_bytes"])
            bound=max(r["disk_bytes"])/config["storage"]["disk_Bps"]
            link=config["storage"].get("shared_link_Bps")
            if link is not None:bound=max(bound,r["bytes"]/link)
            startup=config["storage"]["read_latency_s"] if layer==0 or storage["layer_spec"]["startup"]=="per_layer" else 0
            assert r["read_latency_s"]==startup
            assert r["completed_s"]-r["submitted_s"]+1e-8>=bound+startup
            completed_mono[(rid,layer)]=storage["t0_monotonic_s"]+k*r["completed_s"]
    counts=Counter();overlaps=0;total_stall=0.;compute_total=0.;batch_hist=Counter()
    for path in sorted(Path(folder).glob("worker*.steps.jsonl")):
        rows=[json.loads(s) for s in path.read_text().splitlines()]
        events=defaultdict(dict)
        for r in rows:
            if r.get("event") in ("layer_compute_start","layer_compute_complete"):
                key=(r["step"],r["layer"],r["event"])
                assert key not in events
                events[key]=r
        previous_end=None
        for step in (r for r in rows if r.get("num_scheduled_tokens")):
            ids=list(step["num_scheduled_tokens"])
            batch_hist[len(ids)]+=1
            timing=step["timing"]
            if timing["mode"]=="sa_e26f":
                from check_sa_stack import manual_layer
                durations=[float(manual_layer(timing["shapes"],config["compute"]["profile"]))]*layers
            elif timing["mode"]=="glm5_d950pr":
                ref=manual_timing(timing)
                durations=[ref["dense_layer_us"]*1e-6]*3+[ref["moe_layer_us"]*1e-6]*75
                durations[0]+=timing["engine_overhead_s"]
            else:
                overhead=config["compute"]["step_overhead_s"]
                durations=[(timing["duration_s"]-overhead)/layers]*layers;durations[0]+=overhead
            assert math.isclose(math.fsum(durations),timing["duration_s"],rel_tol=1e-12,abs_tol=1e-12)
            for layer,duration in enumerate(durations):
                start=events.pop((step["step"],layer,"layer_compute_start"))
                end=events.pop((step["step"],layer,"layer_compute_complete"))
                assert start["req_ids"]==end["req_ids"]==ids
                assert (end["occurred_mono"]-start["occurred_mono"])/k+1e-6>=duration
                assert math.isclose(start["duration_model_s"],duration,rel_tol=1e-12,abs_tol=1e-12)
                assert all(start["occurred_mono"]+1e-7>=completed_mono.get((rid,layer),-math.inf) for rid in ids)
                if previous_end is not None:
                    assert start["occurred_mono"]>=previous_end
                    total_stall+=(start["occurred_mono"]-previous_end)/k
                previous_end=end["occurred_mono"]
                compute_total+=duration
                counts["layers"]+=1
                for rid in ids:
                    next_read=actual.get((rid,layer+1))
                    # Only the first step that loads this prefix owns these IOs.
                    if next_read is None:continue
                    submitted=storage["t0_monotonic_s"]+k*next_read["submitted_s"]
                    if submitted < start["occurred_mono"]-1e-7:continue
                    if config["kv_transfer_mode"]=="layer_pipeline":
                        assert submitted<=end["occurred_mono"]+1e-7
                        overlaps+=1
                    else:assert submitted>=end["occurred_mono"]-1e-7
            counts["steps"]+=1
        assert not events
    return dict(status="pass",mode=config["kv_transfer_mode"],requests_with_loads=len(loads),
        layer_reads=len(history),layer_computes=counts["layers"],steps=counts["steps"],
        prefetches_within_compute=overlaps,compute_model_s=compute_total,
        between_layer_and_step_wait_model_s=total_stall,batch_histogram=dict(batch_hist))


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    folder=parser.parse_args().folder
    print(json.dumps(check(folder,json.loads((folder/"config.json").read_text()),
                           json.loads((folder/"storage.final.json").read_text())),indent=2))
