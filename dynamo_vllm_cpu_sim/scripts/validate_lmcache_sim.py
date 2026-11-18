#!/usr/bin/env python3
"""Real Dynamo/vLLM smoke test for the optional LMCache CPU adapter."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"scripts")]


def main():
    from run_tfs_stack import run_one
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-vllm-hashes", action="store_true")
    parser.add_argument("--modes", nargs="+", choices=["immediate", "tfs"], default=["immediate", "tfs"])
    args = parser.parse_args()
    if os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("set PYTHONHASHSEED=0")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT/"configs/mooncake_first_seen_layerwise.json").read_text())
    config.update(block_size=16, max_model_len=2048, num_blocks=8193,
                  max_num_batched_tokens=64, wall_time_scale=10,
                  kv_connector_implementation="lmcache")
    config["storage"].update(completion_mode="notify", disks=4, disk_Bps=25e9, read_latency_s=0.)
    config["admission"].update(feedback_delay_s=0, execution_feedback="disabled")
    config["lmcache_sim"] = dict(reuse_vllm_hashes=args.reuse_vllm_hashes)
    rng, rows = random.Random(418), []
    for i, kind in enumerate("ABACABCA"):
        h, u = {"A":(512, 1), "B":(32, 97), "C":(0, 129)}[kind]
        rows.append(dict(id=f"lmcache{i:02}", arrival_s=0.,
            prompt_token_ids=[rng.choice([1332,995,2581,640,1366,23748]) for _ in range(h+u)],
            output_tokens=1, remote_prefix_tokens=h, hit_tokens=h, u_tokens=u,
            full_hit_adjusted=u==1, cache_model="first_seen", class_id=kind))
    trace = out/"trace.jsonl"
    trace.write_text("".join(json.dumps(row)+"\n" for row in rows))
    results = []
    for mode in args.modes:
        cfg = deepcopy(config)
        if mode == "tfs":
            cfg["admission"].update(placement="scheduler", classification="arrival", aging_enabled=False)
        check = run_one(cfg, trace, out/mode, Path(sys.executable), mode, "first_token", 2)
        storage = json.loads((out/mode/"storage.final.json").read_text())
        layer = json.loads((out/mode/"layer_check.json").read_text())
        assert layer["layer_reads"] == 468
        assert storage["storage_completion"]["error"] is None
        if args.reuse_vllm_hashes:
            counters = [json.loads(p.read_text()) for p in (out/mode).glob("*.lmcache.hash.json")]
            assert len(counters) == 2 and sum(c["reused_chunks"] for c in counters) > 0
            assert sum(c["computed_chunks"] for c in counters) == 0
        results.append(dict(mode=mode, check=check, layer_check=layer))
        (out/"report.json").write_text(json.dumps(dict(status="passed", runs=results), indent=2)+"\n")


if __name__ == "__main__":
    main()
