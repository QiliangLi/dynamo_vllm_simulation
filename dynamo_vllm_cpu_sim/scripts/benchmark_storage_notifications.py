#!/usr/bin/env python3
"""Replay frozen IO-bearing batches through real storage sockets/processes.

This isolates the connector/storage path. It preserves worker assignments,
batch membership, layer compute durations, disk counts and read demand. Workers
start together and drain their fixed queues; original arrival/admission gaps
are not replayed. No Dynamo router, Scheduler or TFS decision runs here.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gc
import json
import multiprocessing as mp
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def percentile(values, fraction):
    return sorted(values)[min(len(values)-1, int((len(values)-1)*fraction))] if values else None


def check_native_hash_inputs(fixture):
    """Outside timing: verify frozen native hashes against real LMCache hashes."""
    from sim.stack.lmcache_bridge import load_runtime
    rt = load_runtime(fixture["config"])
    chunk = fixture["config"]["block_size"]
    config = rt.LMCacheEngineConfig.from_defaults(chunk_size=chunk, pre_caching_hash_algorithm="sha256")
    metadata = rt.LMCacheEngineMetadata("check", 1, 0, "vllm", rt.torch.float32, (1, 2, chunk, 1, 1))
    database = rt.ChunkedTokenDatabase(config, metadata)
    requests, chunks = 0, 0
    for batches in fixture["batches"].values():
        for batch in batches:
            for rid in batch["rids"]:
                supplied = [bytes.fromhex(h) for h in batch["native_hashes"][rid]]
                tokens = batch["prompts"][rid][:len(supplied)*chunk]
                calculated = [key.chunk_hash for _,_,key in database.process_tokens(tokens=tokens)]
                assert calculated == supplied, (rid, "native/LMCache hash mismatch")
                requests += 1
                chunks += len(supplied)
    return dict(status="passed", requests=requests, chunks=chunks)


def prepare(source, include_prompts=False):
    storage = json.loads((source/"storage.final.json").read_text())
    config = json.loads((source/"config.json").read_text())
    measured = set(json.loads((source/"measured/report.json").read_text())["requests"])
    ids = {r["rid"] for r in storage["layer_read_history"]
           if storage["hit_bindings"].get(r["rid"].replace("-", "").lower()[:32]) in measured}
    history = [r for r in storage["layer_read_history"] if r["rid"] in ids]
    loads = {}
    for rid in ids:
        row = storage["layer_loads"][rid]
        loads[rid] = {k:deepcopy(row[k]) for k in ("worker", "counts", "demand", "layer_bytes", "total_bytes")}
        loads[rid].update(blocks_hex=[], submitted_layers=[], completed_layers=[],
                          submitted_bytes=0, completed_bytes=0)
        loads[rid]["counts"] = {int(k):v for k,v in loads[rid]["counts"].items()}
    batches = {}
    assigned = set()
    for file in sorted(source.glob("worker*.steps.jsonl")):
        worker = int(file.name.split(".")[0][6:])
        batches[worker] = []
        for line in file.open():
            row = json.loads(line)
            if not row.get("num_scheduled_tokens"):
                continue
            new = [rid for rid in row["num_scheduled_tokens"] if rid in ids and rid not in assigned]
            if not new:
                continue
            assert all(loads[rid]["worker"] == worker for rid in new)
            batches[worker].append(dict(rids=new, timing=row["timing"]))
            assigned.update(new)
    assert assigned == ids
    assert len(history) == len(ids)*storage["layer_spec"]["layers"]
    # The trusted fixture reuses the already validated HRW disk counts. It does
    # not expose a client API accepting arbitrary placement or bypass real runs.
    config.pop("cache_model", None)
    config["enable_prefix_caching"] = False
    fixture = dict(config=config, loads=loads, batches=batches,
                   reads=len(history), bytes=sum(r["bytes"] for r in history), source=str(source))
    if include_prompts:
        traces = {r["id"]:r for r in (json.loads(line) for line in (source/"trace.jsonl").open())}
        prompts = {rid:traces[storage["hit_bindings"][rid.replace("-", "").lower()[:32]]]["prompt_token_ids"]
                   for rid in ids}
        for worker, queue in batches.items():
            for batch in queue:
                batch["prompts"] = {rid:prompts[rid] for rid in batch["rids"]}
                batch["native_hashes"] = {rid:storage["layer_loads"][rid]["blocks_hex"] for rid in batch["rids"]}
    return fixture


def server_main(config, loads, path, ready):
    from sim.stack.storage_service import Service, handle
    service = Service(config, [])
    service.layer_state.loads = deepcopy(loads)
    service.gc_window.begin()
    service.start()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(64)
    ready.put(dict(t0=service.t0))
    try:
        while True:
            conn, _ = listener.accept()
            def serve(peer):
                try:
                    with peer:
                        handle(peer, service)
                except (OSError, ConnectionError):
                    pass
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    finally:
        service.close()


def worker_main(worker, batches, loads, config, path, start, ready, result, t0):
    from types import SimpleNamespace
    from sim.stack.clock import TimeScale
    from sim.stack.layerwise import LayerRunner
    from sim.stack.storage_client import StorageClient
    client = None
    try:
        gc.disable()
        client = StorageClient(path)
        notify = config["storage"]["completion_mode"] == "notify"
        lmcache = config.get("kv_connector_implementation") == "lmcache"
        if notify:
            client.prepare_layers(worker, [], 0)
        rpc = defaultdict(list)
        original = client._call
        def call(payload):
            begin = time.perf_counter()
            try:
                return original(payload)
            finally:
                rpc[payload["op"]].append(time.perf_counter()-begin)
        client._call = call
        observations = []
        def emit(event, **fields):
            if event == "layer_read_observed":
                observations.append(time.monotonic() - t0 -
                                    config.get("wall_time_scale", 1.)*fields["completed_model_s"])
        if lmcache:
            from sim.stack.lmcache_bridge import LMCacheRunner, benchmark_vllm_config, load_runtime, provenance
            runner = LMCacheRunner(config, client, worker, TimeScale.from_config(config), None, emit,
                                   benchmark_vllm_config(config))
            rt = load_runtime(config)
            native = provenance(config)
            from vllm.v1.core import kv_cache_utils
            for batch in batches:
                batch["native_hashes"] = {rid:tuple(bytes.fromhex(h) for h in hashes)
                                          for rid,hashes in batch["native_hashes"].items()}
        else:
            runner = LayerRunner(config, client, worker, TimeScale.from_config(config), None, emit)
            native = None
        ready.put(worker)
        if not start.wait(60):
            raise TimeoutError("benchmark start timed out")
        begin, cpu = time.monotonic(), time.process_time()
        for step, batch in enumerate(batches, 1):
            if lmcache:
                metadata = rt.adapter.LMCacheConnectorMetadata()
                for rid in batch["rids"]:
                    h = loads[rid]["total_bytes"]//config["storage"]["kv_bytes_per_token"]
                    # The worker only loads the hit range. Real scheduler
                    # metadata is validated separately by validate_lmcache_sim.
                    tokens = batch["prompts"][rid][:h+1]
                    metadata.requests.append(rt.adapter.ReqMeta(rid, tokens, rt.torch.arange(len(tokens)),
                        load_spec=rt.adapter.LoadSpec(0, h, True),
                        save_spec=rt.adapter.SaveSpec(h, False)))
                metadata.loads = {rid:loads[rid] for rid in batch["rids"]}
                metadata.native_hashes = {rid:dict(hashes=batch["native_hashes"][rid],
                    block_size=config["block_size"], algorithm="sha256", plain_text=True,
                    initial_hash=kv_cache_utils.NONE_HASH) for rid in batch["rids"]}
            else:
                metadata = SimpleNamespace(loads={rid:loads[rid] for rid in batch["rids"]})
            runner.install(metadata)
            runner.run(batch["rids"], batch["timing"], step)
            for rid in batch["rids"]:
                runner.forget(rid)
        end, elapsed_cpu = time.monotonic(), time.process_time()-cpu
        if lmcache:
            hash_stats = runner.hash_stats()
            runner.close()
        else:
            hash_stats = None
        result.put(dict(worker=worker, begin=begin, end=end,
                        cpu_s=elapsed_cpu, rpc=dict(rpc), observations=observations, native=native,
                        hash_stats=hash_stats))
    except BaseException:
        result.put(dict(worker=worker, error=traceback.format_exc()))
    finally:
        if client is not None:
            client.close()


def audit(config, snapshot, expected):
    from sim.grouped_storage import GroupedStorage
    store = GroupedStorage(config["storage"], config["block_size"])
    done = {}
    def advance(target):
        for key in store.advance(target):
            done[key] = store.now
    history = snapshot["layer_read_history"]
    assert len(history) == expected["reads"]
    assert sum(r["bytes"] for r in history) == expected["bytes"]
    assert snapshot["pending_reads"] == 0
    for row in history:
        while store.next_time() <= row["submitted_s"]:
            advance(store.next_time())
        advance(row["submitted_s"])
        load = snapshot["layer_loads"][row["rid"]]
        counts = {int(k):v for k,v in load["counts"].items()}
        key = (row["worker"], row["rid"], row["layer"])
        store.submit_counted(store.now, key, counts, row["bytes"]/sum(counts.values()),
                             load["demand"], row["read_latency_s"])
    while store.pending:
        advance(store.next_time())
    errors = [
        abs(done[(r["worker"], r["rid"], r["layer"])]-r["completed_s"]) for r in history]
    assert max(errors) < 1e-7, max(errors)
    assert abs(snapshot["bytes_transferred"]-expected["bytes"]) < 1.
    assert max(abs(a-b) for a,b in zip(snapshot["disk_bytes_transferred"], store.disk_bytes_transferred)) < 1.
    assert all(load["submitted_layers"] == load["completed_layers"] == list(range(snapshot["layer_spec"]["layers"]))
               for load in snapshot["layer_loads"].values())
    return dict(status="passed", reads=len(history), bytes=expected["bytes"], max_completion_error_s=max(errors),
                disk_bytes=snapshot["disk_bytes_transferred"])


def run_case(fixture, mode, folder):
    from sim.stack.storage_client import StorageClient
    ctx = mp.get_context("spawn")
    config = deepcopy(fixture["config"])
    is_lmcache = mode in ("lmcache", "lmcache_hash")
    config["storage"]["completion_mode"] = "notify" if is_lmcache or mode == "single_writer" else mode
    config["storage"]["service_mode"] = "single_writer" if mode == "single_writer" else "locked"
    config["kv_connector_implementation"] = "lmcache" if is_lmcache else "sim"
    config.setdefault("lmcache_sim", {})["reuse_vllm_hashes"] = mode == "lmcache_hash"
    config["storage"]["record_reads"] = True
    ready, results, started = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = []
    folder.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="kv-notify-") as tmp:
        path = tmp+"/storage.sock"
        server = ctx.Process(target=server_main, args=(config, fixture["loads"], path, ready))
        server.start()
        try:
            t0 = ready.get(timeout=90)["t0"]
            for worker, batches in fixture["batches"].items():
                proc = ctx.Process(target=worker_main, args=(worker, batches, fixture["loads"], config,
                                   path, started, ready, results, t0))
                proc.start()
                processes.append(proc)
            for _ in processes:
                ready.get(timeout=30)
            import os
            def service_cpu():
                ticks = Path(f"/proc/{server.pid}/stat").read_text().split()
                return (int(ticks[13])+int(ticks[14]))/os.sysconf("SC_CLK_TCK")
            cpu_before = service_cpu()
            begin = time.monotonic()
            started.set()
            rows = [results.get(timeout=90) for _ in processes]
            errors = [row for row in rows if "error" in row]
            if errors:
                raise RuntimeError(errors)
            elapsed = max(row["end"] for row in rows)-begin
            for proc in processes:
                proc.join(10)
                assert proc.exitcode == 0, proc.exitcode
            client = StorageClient(path)
            snapshot = client.stats()
            client.close()
            # Linux process CPU includes all service handler/progress threads.
            cpu_total = service_cpu()-cpu_before
            check = audit(config, snapshot, fixture)
            rpc = defaultdict(list)
            for row in rows:
                for op, values in row["rpc"].items():
                    rpc[op].extend(values)
            delays = [max(0., value) for row in rows for value in row["observations"]]
            summary = dict(mode=mode, wall_s=elapsed, client_cpu_s=sum(r["cpu_s"] for r in rows),
                           service_cpu_s=cpu_total, observations=len(delays),
                           observation_delay_mean_ms=statistics.mean(delays)*1000,
                           observation_delay_p95_ms=percentile(delays, .95)*1000,
                           rpc={op:dict(count=len(values), mean_ms=statistics.mean(values)*1000,
                                       p95_ms=percentile(values,.95)*1000) for op,values in rpc.items()},
                           progress=snapshot.get("storage_completion", dict(mode="poll")), audit=check,
                           hash_stats=dict(sum((Counter(r["hash_stats"]) for r in rows if r["hash_stats"]), Counter())))
            write(folder/"report.json", summary)
            write(folder/"storage.final.json", snapshot)
            write(folder/"workers.json", rows)
            return summary
        finally:
            for proc in processes+[server]:
                if proc.is_alive():
                    proc.terminate()
                proc.join(10)
                if proc.is_alive():
                    proc.kill()
                    proc.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit-batches", type=int, help="smoke test only")
    parser.add_argument("--modes", nargs="+", choices=["poll", "notify", "single_writer", "lmcache", "lmcache_hash"], default=["poll", "notify"])
    args = parser.parse_args()
    if args.repeats < 1 or len(set(args.modes)) != len(args.modes):
        parser.error("positive repeats and distinct modes required")
    fixture = prepare(args.source_run, include_prompts=any(m.startswith("lmcache") for m in args.modes))
    if args.limit_batches:
        fixture["batches"] = {w:b[:args.limit_batches] for w,b in fixture["batches"].items()}
        ids = {rid for batches in fixture["batches"].values() for b in batches for rid in b["rids"]}
        fixture["loads"] = {r:v for r,v in fixture["loads"].items() if r in ids}
        layers = fixture["config"]["layerwise"]["layers"]
        fixture["reads"] = len(ids)*layers
        fixture["bytes"] = sum(load["total_bytes"] for load in fixture["loads"].values())
    hash_input_check = check_native_hash_inputs(fixture) if "lmcache_hash" in args.modes else None
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output/"fixture.json", fixture)
    results = []
    for repeat in range(args.repeats):
        for mode in (args.modes if repeat%2 == 0 else list(reversed(args.modes))):
            print(json.dumps(dict(event="start", repeat=repeat+1, mode=mode)), flush=True)
            row = run_case(fixture, mode, args.output/f"r{repeat+1}-{mode}")
            row["repeat"] = repeat+1
            results.append(row)
            write(args.output/"progress.json", dict(runs=results))
            print(json.dumps(row), flush=True)
    medians = {mode:statistics.median(r["wall_s"] for r in results if r["mode"] == mode)
               for mode in args.modes}
    comparison = ((1-medians[args.modes[-1]]/medians[args.modes[0]])*100 if len(args.modes)>1 else None)
    write(args.output/"report.json", dict(status="passed", fixture_reads=fixture["reads"],
          fixture_bytes=fixture["bytes"], workers=len(fixture["batches"]), runs=results,
          median_s=medians, reduction_pct=comparison,
          scope=__doc__, hash_input_check=hash_input_check))


if __name__ == "__main__":
    main()
