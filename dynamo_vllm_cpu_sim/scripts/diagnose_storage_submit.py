#!/usr/bin/env python3
"""Opt-in storage timing study; uses the existing model without changing it.

The RPC cases run one storage process and eight independent client processes.
They do not start Dynamo/vLLM engines or move real KV tensors. Timings belong
to this host, not production hardware. All detailed rows are buffered in RAM
and written only after a trial. The original RPC handler is the baseline.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import copy
import cProfile
import gzip
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import pstats
import socket
import statistics
import sys
import tempfile
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def runtime():
    from sim.engine import make_request
    from sim.stack.storage_client import StorageClient
    from sim.stack.storage_service import Service, handle
    return Service, StorageClient, handle, make_request


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def summary_ms(values):
    values = list(values)
    return dict(count=len(values), median_ms=percentile(values, .5),
                p95_ms=percentile(values, .95), max_ms=max(values, default=None))


def peak_overlap(reads):
    events = []
    for row in reads:
        if row.get("completed_s") is None:
            raise ValueError("read has no completion")
        events.extend([(row["submitted_s"], 1), (row["completed_s"], -1)])
    active = peak = 0
    for _, delta in sorted(events):  # completion before admission on a tie
        active += delta
        peak = max(peak, active)
    return peak


def prepare(config, workers, blocks, history):
    Service, _, _, make_request = runtime()
    cfg = copy.deepcopy(config)
    if cfg["block_size"] != 1:
        raise ValueError("this isolation study requires the original 1-token configuration")
    rows = [dict(id=f"probe-{w}", arrival_s=0.,
                 prompt_token_ids=[100 + w] + [3] * blocks, output_tokens=1,
                 remote_prefix_tokens=blocks, hit_tokens=blocks, u_tokens=1,
                 full_hit_adjusted=True, cache_model="first_seen")
            for w in range(workers)]
    service = Service(cfg, rows)
    specs = []
    for w, row in enumerate(rows):
        rid = str(uuid.UUID(int=w + 1)) + "-probe"
        hashes = list(make_request(row, 1, "fcfs").block_hashes[:blocks])
        service.op_bind(rid, row["id"], row["prompt_token_ids"], blocks)
        # Submit-only cases exclude query work explicitly. Full-chain cases
        # invoke the real query again after their start barrier.
        service._matched[rid] = hashes
        specs.append(dict(worker=w, rid=rid, blocks_hex=[h.hex() for h in hashes]))
    demand = 1e8 / blocks
    for i in range(history):
        service.store.submit(service.store.now, (0, f"history-{i}"),
                             [bytes.fromhex(h) for h in specs[0]["blocks_hex"]], demand)
        while service.store.pending:
            service.store.advance(service.store.next_time())
    # Seeding is outside the measured window. Preserve model now, but map it
    # to the current monotonic time so historical virtual time is not future.
    service.t0 = time.monotonic() - service.clock.to_wall(service.store.now)
    return service, specs, demand


class Instrumentation:
    """Wrap coarse calls only; no per-block tracing, hashing or file writes."""
    def __init__(self, service):
        self.service, self.rows, self.completions = service, [], []
        self.current = None  # only accessed under the service's existing lock
        original_advance = service._advance_to_wall
        original_submit = service.store.submit
        original_drain = service._drain_done

        def advance():
            start = time.monotonic_ns()
            try:
                return original_advance()
            finally:
                if self.current is not None:
                    self.current["advance_ns"] += time.monotonic_ns() - start

        def submit(*args, **kwargs):
            start = time.monotonic_ns()
            try:
                return original_submit(*args, **kwargs)
            finally:
                if self.current is not None:
                    self.current["model_submit_ns"] += time.monotonic_ns() - start

        def drain(done):
            observed = time.monotonic_ns()
            for worker, rid in done:
                self.completions.append(dict(worker=worker, rid=rid,
                    completed_s=service.store.now, observed_ns=observed))
            return original_drain(done)

        service._advance_to_wall, service.store.submit, service._drain_done = advance, submit, drain

    def begin(self, op, rid, **extra):
        self.current = dict(op=op, rid=rid, advance_ns=0, model_submit_ns=0,
                            queue_entries_before=len(self.service.store.queues), **extra)
        return self.current

    def end(self, row):
        row["queue_entries_after"] = len(self.service.store.queues)
        row["model_now_s"] = self.service.store.now
        row["pending_after"] = len(self.service.store.pending)
        self.current = None


def dispatch(service, req):
    op = req["op"]
    if op == "submit":
        return service.op_submit(req["worker"], req["rid"], req["blocks_hex"], req["demand"])
    if op == "present_for_request":
        return service.op_present_for_request(req["rid"], req["start_block"], req["blocks_hex"])
    if op == "finished":
        return service.op_finished(req["worker"])
    raise ValueError(op)


def timed_handle(conn, service, inst):
    """Same decode/lock/dispatch/send order as the original handle()."""
    buf = b""
    while True:
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        framed = time.monotonic_ns()  # userspace frame, NOT kernel arrival
        line, buf = buf.split(b"\n", 1)
        req = json.loads(line)
        decoded = time.monotonic_ns()
        with service._lock:
            acquired = time.monotonic_ns()
            row = inst.begin(req["op"], req.get("rid"), framed_ns=framed,
                             decoded_ns=decoded, acquired_ns=acquired,
                             worker=req.get("worker"), sequence=req.get("probe_seq"))
            try:
                resp = dispatch(service, req)
            except Exception as exc:
                resp = dict(ok=False, error=f"{type(exc).__name__}: {exc}")
            inst.end(row)
            row["operation_done_ns"] = time.monotonic_ns()
        row["unlocked_ns"] = time.monotonic_ns()
        encoded = (json.dumps(resp) + "\n").encode()
        row["response_encoded_ns"] = time.monotonic_ns()
        conn.sendall(encoded)
        row["sent_ns"] = time.monotonic_ns()
        inst.rows.append(row)


def collect(service, initial_bytes, inst=None):
    stats = service.op_stats()
    history = [{k: v for k, v in r.items() if k != "blocks_hex"}
               for r in stats.get("read_history", [])]
    return dict(peak_pending=stats["peak_pending_requests"],
                t0_monotonic_s=service.t0,
                wall_time_scale=service.clock.factor,
                bytes_transferred=stats["bytes_transferred"] - initial_bytes,
                bytes_submitted=stats["bytes_submitted"],
                pending=stats["pending_reads"], queue_entries=len(service.store.queues),
                reads=history, server_rows=inst.rows if inst else [],
                completions=inst.completions if inst else [])


def direct_trial(config, workers, blocks, history, detail):
    service, specs, demand = prepare(config, workers, blocks, history)
    initial_bytes = service.store.bytes_transferred
    inst = Instrumentation(service) if detail else None
    barrier = threading.Barrier(workers + 1)

    def run(spec):
        barrier.wait(timeout=30)
        called = time.monotonic_ns()
        with service._lock:
            acquired = time.monotonic_ns()
            row = inst.begin("submit", spec["rid"]) if inst else None
            service.op_submit(spec["worker"], spec["rid"], spec["blocks_hex"], demand)
            if inst:
                inst.end(row)
            done = time.monotonic_ns()
        return dict(worker=spec["worker"], rid=spec["rid"], called_ns=called,
                    acquired_ns=acquired, done_ns=done,
                    detail=row)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, spec) for spec in specs]
        barrier.wait(timeout=30)
        calls = [f.result(timeout=30) for f in futures]
    deadline = time.monotonic() + 30
    while service.store.pending:
        if time.monotonic() > deadline:
            raise TimeoutError("direct model did not drain")
        service.op_finished(0)
        time.sleep(.0002)
    return dict(**collect(service, initial_bytes, inst), direct_calls=calls)


def serve(pipe, path, config, workers, blocks, history, detail):
    try:
        service, specs, demand = prepare(config, workers, blocks, history)
        _, _, original_handle, _ = runtime()
        inst = Instrumentation(service) if detail else None
        initial_bytes = service.store.bytes_transferred
        threads = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen(16)
            listener.settimeout(.02)
            pipe.send(dict(ready=True, specs=specs, demand=demand))

            def connected(conn):
                with conn:
                    if inst:
                        timed_handle(conn, service, inst)
                    else:
                        original_handle(conn, service)

            while not pipe.poll():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                thread = threading.Thread(target=connected, args=(conn,), daemon=True)
                thread.start()
                threads.append(thread)
            if pipe.recv() != "collect":
                raise ValueError("bad control command")
            for thread in threads:
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("RPC client still connected")
            pipe.send(collect(service, initial_bytes, inst))
    except BaseException:
        pipe.send(dict(error=traceback.format_exc()))
        raise
    finally:
        pipe.close()


def rpc_client(pipe, path, spec, demand, barrier, detail, query):
    try:
        # Import the lightweight real client; avoid importing the engine here.
        from sim.stack.storage_client import StorageClient
        client = StorageClient(path)
        rows = []
        if detail:
            def measured_call(payload):
                sequence = len(rows)
                payload = dict(payload, probe_seq=sequence)
                called = time.monotonic_ns()
                encoded = (json.dumps(payload) + "\n").encode()
                encoded_at = time.monotonic_ns()
                client.sock.sendall(encoded)
                sent = time.monotonic_ns()
                while b"\n" not in client._buf:
                    chunk = client.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("storage closed")
                    client._buf += chunk
                received = time.monotonic_ns()
                line, client._buf = client._buf.split(b"\n", 1)
                response = json.loads(line)
                decoded = time.monotonic_ns()
                rows.append(dict(worker=spec["worker"], sequence=sequence,
                    op=payload["op"], called_ns=called, encoded_ns=encoded_at,
                    sent_ns=sent, received_ns=received, decoded_ns=decoded))
                if not response.get("ok", True):
                    raise RuntimeError(response)
                return response
            client._call = measured_call
        hashes = [bytes.fromhex(h) for h in spec["blocks_hex"]]
        barrier.wait(timeout=30)
        started = time.monotonic_ns()
        if query:
            hits = client.present_for_request(spec["rid"], 0, hashes)
            assert all(hits)
        query_done = time.monotonic_ns()
        client.submit(spec["worker"], spec["rid"], hashes, demand)
        submitted = time.monotonic_ns()
        deadline = time.monotonic() + 30
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("completion not received")
            finished = client.finished(spec["worker"])
            if spec["rid"] in finished:
                break
            time.sleep(.0002)
        completed = time.monotonic_ns()
        client.close()
        pipe.send(dict(worker=spec["worker"], rid=spec["rid"],
            started_ns=started, query_done_ns=query_done, submitted_ns=submitted,
            completed_ns=completed, calls=rows))
    except BaseException:
        pipe.send(dict(error=traceback.format_exc()))
        raise
    finally:
        pipe.close()


def receive(pipe, timeout=90):
    if not pipe.poll(timeout):
        raise TimeoutError("diagnostic child did not respond")
    result = pipe.recv()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def rpc_trial(config, workers, blocks, history, detail, query=False):
    context = mp.get_context("spawn")
    children = []
    with tempfile.TemporaryDirectory(prefix="st-", dir="/tmp") as tmp:
        path = str(Path(tmp) / "s")
        parent, child = context.Pipe()
        server = context.Process(target=serve,
            args=(child, path, config, workers, blocks, history, detail))
        server.start()
        children.append(server)
        child.close()
        try:
            ready = receive(parent)
            barrier = context.Barrier(workers + 1)
            pipes = []
            for spec in ready["specs"]:
                p, c = context.Pipe()
                process = context.Process(target=rpc_client,
                    args=(c, path, spec, ready["demand"], barrier, detail, query))
                process.start()
                c.close()
                children.append(process)
                pipes.append(p)
            barrier.wait(timeout=60)
            clients = [receive(p) for p in pipes]
            for p in pipes:
                p.close()
            for process in children[1:]:
                process.join(timeout=10)
                if process.exitcode != 0:
                    raise RuntimeError("client did not exit cleanly")
            parent.send("collect")
            result = receive(parent)
            server.join(timeout=10)
            if server.exitcode != 0:
                raise RuntimeError("server did not exit cleanly")
            return dict(**result, clients=clients)
        finally:
            parent.close()
            for process in children:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=10)


def summarize(trial):
    metrics = defaultdict(list)
    for row in trial.get("direct_calls", []):
        metrics["lock_wait"].append((row["acquired_ns"] - row["called_ns"]) / 1e6)
        metrics["operation"].append((row["done_ns"] - row["acquired_ns"]) / 1e6)
        for name in ("advance_ns", "model_submit_ns"):
            if row["detail"]:
                metrics[name[:-3]].append(row["detail"][name] / 1e6)
    servers = {}
    for row in trial.get("server_rows", []):
        metrics[row["op"] + ".operation"].append(
            (row["operation_done_ns"] - row["acquired_ns"]) / 1e6)
        metrics[row["op"] + ".lock_wait"].append(
            (row["acquired_ns"] - row["decoded_ns"]) / 1e6)
        if row["op"] != "submit":
            continue
        servers[(row["worker"], row["sequence"])] = row
        metrics["lock_wait"].append((row["acquired_ns"] - row["decoded_ns"]) / 1e6)
        metrics["operation"].append((row["operation_done_ns"] - row["acquired_ns"]) / 1e6)
        metrics["advance"].append(row["advance_ns"] / 1e6)
        metrics["model_submit"].append(row["model_submit_ns"] / 1e6)
        metrics["server_json_decode"].append((row["decoded_ns"] - row["framed_ns"]) / 1e6)
        metrics["other_operation"].append((row["operation_done_ns"] - row["acquired_ns"]
                                           - row["advance_ns"] - row["model_submit_ns"]) / 1e6)
    for client in trial.get("clients", []):
        metrics["submit_call"].append((client["submitted_ns"] - client["query_done_ns"]) / 1e6)
        metrics["query_call"].append((client["query_done_ns"] - client["started_ns"]) / 1e6)
        for row in client["calls"]:
            if row["op"] != "submit":
                continue
            metrics["client_json_encode"].append((row["encoded_ns"] - row["called_ns"]) / 1e6)
            server = servers[(row["worker"], row["sequence"])]
            # This residual includes socket transport, wakeups and scheduling;
            # it is NOT a pure network latency measurement.
            rtt = row["decoded_ns"] - row["called_ns"]
            service_span = server["response_encoded_ns"] - server["framed_ns"]
            metrics["rpc_residual"].append((rtt - service_span) / 1e6)
        match = next((r for r in trial["reads"] if r["rid"] == client["rid"]), None)
        if match:
            metrics["model_read"].append((match["completed_s"] - match["submitted_s"]) * 1000)
            absolute_done_ns = round((trial["t0_monotonic_s"]
                                      + match["completed_s"] * trial.get("wall_time_scale",1.)) * 1e9)
            metrics["model_done_to_client"].append((client["completed_ns"] - absolute_done_ns) / 1e6)
            observed = next((r for r in trial["completions"] if r["rid"] == client["rid"]), None)
            if observed:
                metrics["model_done_to_service_observed"].append(
                    (observed["observed_ns"] - absolute_done_ns) / 1e6)
    for row in trial.get("direct_calls", []):
        if row["detail"]:
            operation = (row["done_ns"] - row["acquired_ns"]) / 1e6
            metrics["other_operation"].append(operation - row["detail"]["advance_ns"] / 1e6
                                               - row["detail"]["model_submit_ns"] / 1e6)
    return {key: summary_ms(values) for key, values in metrics.items()}


def archived_cases(path):
    data = json.loads(gzip.decompress(Path(path).read_bytes()))
    results = []
    for case in data["cases"]:
        reads = sorted(case["reads"], key=lambda r: r["submitted_s"])
        durations = [(r["completed_s"] - r["submitted_s"]) * 1000 for r in reads]
        gaps = [(b["submitted_s"] - a["submitted_s"]) * 1000 for a, b in zip(reads, reads[1:])]
        results.append(dict(case=case["case"], reads=len(reads),
            peak_from_intervals=peak_overlap(reads), model_read=summary_ms(durations),
            submit_gap=summary_ms(gaps),
            no_overlap_pairs=sum(b["submitted_s"] >= a["completed_s"]
                                 for a, b in zip(reads, reads[1:])),
            limitation="Original data has no per-RPC phase timing; causes cannot be apportioned."))
    return dict(source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(), cases=results)


def profile_calls(config, workers, blocks):
    service, specs, demand = prepare(config, workers, blocks, 0)
    profile = cProfile.Profile()
    profile.enable()
    for spec in specs:
        service.op_submit(spec["worker"], spec["rid"], spec["blocks_hex"], demand)
    profile.disable()
    stats = pstats.Stats(profile)
    rows = []
    for (file, line, name), (primitive, total, own, cumulative, _) in stats.stats.items():
        rows.append(dict(file=file, line=line, name=name, calls=total,
                         own_s=own, cumulative_s=cumulative))
    return dict(limitation="cProfile changes timing; use for call counts and attribution only.",
                functions=sorted(rows, key=lambda r: r["own_s"], reverse=True)[:35])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/mooncake_first_seen.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--blocks", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--history", type=int, default=64)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must not exist")
    if min(args.workers, args.blocks, args.repeats) <= 0 or args.history < 0:
        parser.error("invalid case dimensions")
    args.output.mkdir(parents=True)
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    (args.output / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    files = [Path(__file__), ROOT / "sim/storage.py", ROOT / "sim/stack/storage_service.py",
             ROOT / "sim/stack/storage_client.py"]
    grouped_source = ROOT / "sim/grouped_storage.py"
    if grouped_source.exists():
        files.append(grouped_source)
    files.append(ROOT / "sim/stack/clock.py")
    for source in files:
        target = args.output / "runtime_sources" / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    report = dict(platform=platform.platform(), python=sys.version,
        recorded_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        clock=str(time.get_clock_info("monotonic")), workers=args.workers,
        blocks=args.blocks, repeats=args.repeats, history_requests=args.history,
        source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        boundary="Storage-only synthetic diagnostic. Real Service/Storage and socket clients; "
                 "no Dynamo/vLLM engines, no real KV payload, no production timing calibration.",
        trials=[])
    cases = [
        ("direct_baseline", "direct", False, True, 0, False),
        ("direct_timed", "direct", True, True, 0, False),
        ("direct_no_read_log", "direct", True, False, 0, False),
        ("direct_history", "direct", True, True, args.history, False),
        ("rpc_baseline", "rpc", False, True, 0, False),
        ("rpc_timed", "rpc", True, True, 0, False),
        ("rpc_no_read_log", "rpc", True, False, 0, False),
        ("rpc_history", "rpc", True, True, args.history, False),
        ("rpc_query_submit", "rpc", True, True, 0, True),
    ]
    # Alternate baseline/detail order by trial; never select by outcome.
    with (args.output / "events.jsonl").open("w") as events:
        for repeat in range(args.repeats):
            for name, transport, detail, read_log, history, query in (cases if repeat % 2 == 0 else cases[::-1]):
                case_cfg = copy.deepcopy(cfg)
                case_cfg["storage"]["record_reads"] = read_log
                if transport == "direct":
                    trial = direct_trial(case_cfg, args.workers, args.blocks, history, detail)
                else:
                    trial = rpc_trial(case_cfg, args.workers, args.blocks, history, detail, query)
                expected = args.workers * args.blocks * case_cfg["storage"]["kv_bytes_per_token"]
                assert trial["pending"] == 0
                assert trial["bytes_submitted"] == expected
                assert abs(trial["bytes_transferred"] - expected) < 1
                if trial["reads"]:
                    assert peak_overlap(trial["reads"]) == trial["peak_pending"]
                result = dict(case=name, repeat=repeat, detail=detail, read_log=read_log,
                    history=history, peak_pending=trial["peak_pending"],
                    bytes_transferred=trial["bytes_transferred"], queue_entries=trial["queue_entries"],
                    metrics=summarize(trial))
                report["trials"].append(result)
                events.write(json.dumps(dict(case=name, repeat=repeat, **trial)) + "\n")
                events.flush()  # after the trial, outside all measured paths
                print(json.dumps(result), flush=True)
    report["profile"] = profile_calls(cfg, args.workers, args.blocks)
    if args.observations:
        report["archived"] = archived_cases(args.observations)
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
