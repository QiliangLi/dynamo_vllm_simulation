import argparse, asyncio, hashlib, importlib.metadata, inspect, json, math, os, statistics
from pathlib import Path

os.environ["VLLM_PLUGINS"] = ""
os.environ.setdefault("VLLM_LOGGING_LEVEL", "ERROR")
os.environ.setdefault("VLLM_USE_V2_MODEL_RUNNER", "0")
os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
from sim.engine import Engine, make_request
from sim.router import Router
from sim.storage import Storage


def validate(rows, c):
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("empty workload or duplicate IDs")
    for r in rows:
        n = len(r["prompt_token_ids"])
        remote = r.get("remote_prefix_tokens", 0)
        if (
            not isinstance(r["id"], str)
            or not math.isfinite(r["arrival_s"])
            or r["arrival_s"] < 0
        ):
            raise ValueError("bad id/time")
        if (
            n < 1
            or r["output_tokens"] < 1
            or n + r["output_tokens"] > c["max_model_len"]
        ):
            raise ValueError("bad length")
        if any(t < 0 or t >= 32000 for t in r["prompt_token_ids"]):
            raise ValueError("token ID out of range")
        if remote < 0 or remote > n or remote % c["block_size"]:
            raise ValueError("remote prefix must be block aligned")


async def simulate(c, rows, output_dir):
    validate(rows, c)
    rows = sorted(rows, key=lambda r: (r["arrival_s"], r["id"]))
    store = Storage(c["storage"], c["block_size"])
    engines = [Engine(i, c, store) for i in range(c["workers"])]
    reqs = {r["id"]: make_request(r, c["block_size"], c["policy"]) for r in rows}
    for r in rows:
        store.present.update(
            reqs[r["id"]].block_hashes[
                : r.get("remote_prefix_tokens", 0) // c["block_size"]
            ]
        )
    router = Router()
    stats = {r["id"]: {"arrival_s": r["arrival_s"], "output_tokens": 0} for r in rows}
    events = []
    now = rows[0]["arrival_s"]
    store.now = now
    cursor = completed = 0

    async def consume(e, scheduled):
        nonlocal completed
        for entry in e.complete(scheduled):
            s = stats[entry.request_id]
            if entry.new_token_ids and "first_token_s" not in s:
                s["first_token_s"] = now
                await router.first_token(entry.request_id)
            s["output_tokens"] += len(entry.new_token_ids)
            if entry.finish_reason is not None:
                s["finish_s"] = now
                s["preemptions"] = reqs[entry.request_id].num_preemptions
                completed += 1
                await router.finish(entry.request_id)
        events.append(
            {
                "t": now,
                "event": "step_complete",
                "worker": e.id,
                "scheduled": scheduled[0].num_scheduled_tokens,
                "waiting_remote": [
                    r.request_id
                    for r in e.scheduler.requests.values()
                    if r.status.name == "WAITING_FOR_REMOTE_KVS"
                ],
            }
        )

    try:
        await router.start(c)
        iterations = 0
        while completed < len(rows):
            iterations += 1
            if iterations > 100000:
                raise RuntimeError("event-loop guard; no silent infinite replay")
            while cursor < len(rows) and rows[cursor]["arrival_s"] <= now + 1e-12:
                r = rows[cursor]
                wid = await router.route(r)
                stats[r["id"]]["worker"] = wid
                engines[wid].scheduler.add_request(reqs[r["id"]])
                events.append(
                    {"t": now, "event": "arrival", "id": r["id"], "worker": wid}
                )
                cursor += 1
            for e in engines:
                immediate = e.schedule(now)
                if immediate is not None:
                    await consume(e, immediate)
                    second = e.schedule(now)
                    if second is not None:
                        await consume(e, second)
            if completed == len(rows):
                break
            target = min(
                [
                    rows[cursor]["arrival_s"] if cursor < len(rows) else math.inf,
                    store.next_time(),
                ]
                + [e.inflight[2] for e in engines if e.inflight]
            )
            if not math.isfinite(target) or target < now:
                raise RuntimeError("deadlock: no future event; inspect block capacity")
            for e in engines:
                e.account(target - now)
            recvs = store.advance(target)
            now = target
            for wid, rid in recvs:
                engines[wid].pending_recvs.add(rid)
                events.append(
                    {"t": now, "event": "kv_received", "worker": wid, "id": rid}
                )
            for e in engines:
                if e.inflight and e.inflight[2] <= now + 1e-12:
                    await consume(e, e.inflight)
        for e in engines:
            immediate = e.schedule(now)
            if immediate:
                await consume(e, immediate)
        ttfts = []
        for rid, s in stats.items():
            s["ttft_s"] = s["first_token_s"] - s["arrival_s"]
            ttfts.append(s["ttft_s"])
            s["external_tokens"] = engines[
                s["worker"]
            ].scheduler.connector.external_tokens.get(rid, 0)
            assert s["output_tokens"] == next(
                r["output_tokens"] for r in rows if r["id"] == rid
            )
        span = now - rows[0]["arrival_s"]
        source = Path(inspect.getfile(type(engines[0].scheduler)))
        report = {
            "status": "completed",
            "scope": "real Dynamo cache-blind selector + real scheduler; synthetic timing",
            "versions": {
                p: importlib.metadata.version(p)
                for p in ("ai-dynamo-runtime", "vllm", "torch")
            },
            "source_pins": json.loads(
                (Path(__file__).resolve().parents[1] / "versions.json").read_text()
            ),
            "workload_sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "scheduler_file": str(source),
            "scheduler_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "config": c,
            "requests_completed": completed,
            "makespan_s": span,
            "throughput_req_s": completed / span,
            "mean_ttft_s": statistics.mean(ttfts),
            "p95_ttft_s": sorted(ttfts)[math.ceil(0.95 * len(ttfts)) - 1],
            "ttft_slo_attainment": sum(t <= c["ttft_slo_s"] for t in ttfts)
            / len(ttfts),
            "bytes_read": round(store.bytes_transferred),
            "workers": [
                {
                    "id": e.id,
                    "compute_s": e.compute_s,
                    "stall_s": e.stall_s,
                    "idle_s": e.idle_s,
                    "steps": e.steps,
                    "unfinished": e.scheduler.get_num_unfinished_requests(),
                    "free_blocks": e.scheduler.kv_cache_manager.block_pool.free_block_queue.num_free_blocks,
                }
                for e in engines
            ],
            "requests": stats,
            "router_loads_after_finish": router.service.loads(),
            "limitations": [
                "No numerical device execution or full EngineCore loop",
                "Dynamo KV-event bridge disabled",
                "Ascend BalanceScheduler only; single DP rank per worker",
                "No layerwise overlap, PD, writeback, cancellation or load failures",
                "Timing coefficients are examples, not NPU calibration",
                "Native equal-cost routing tie can be random",
            ],
        }
        assert (
            not store.pending
            and completed == len(rows)
            and all(e.scheduler.get_num_unfinished_requests() == 0 for e in engines)
        )
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        (out / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events))
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "status",
                        "requests_completed",
                        "makespan_s",
                        "mean_ttft_s",
                        "bytes_read",
                    )
                },
                indent=2,
            )
        )
        return report
    finally:
        await router.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/demo.json")
    p.add_argument("--trace", default="configs/demo.jsonl")
    p.add_argument("--output", default="results/demo")
    p.add_argument(
        "--scheduler", choices=["upstream", "ascend_default", "ascend_balance"]
    )
    p.add_argument("--policy", choices=["fcfs", "priority"])
    a = p.parse_args()
    c = json.loads(Path(a.config).read_text())
    if a.scheduler:
        c["scheduler"] = a.scheduler
    if a.policy:
        c["policy"] = a.policy
    rows = [json.loads(x) for x in Path(a.trace).read_text().splitlines() if x.strip()]
    asyncio.run(simulate(c, rows, a.output))


if __name__ == "__main__":
    main()
