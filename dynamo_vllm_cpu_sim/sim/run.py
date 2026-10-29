import heapq
from dataclasses import asdict, replace
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


from sim.admission import AdmissionController
from sim.request_profile import normalize_config, build_profile
from sim.tfs_metrics import summarize


def validate(rows, c):
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("empty workload or duplicate IDs")
    for r in rows:
        if c["compute"].get("mode") == "glm5_d950pr" and r["output_tokens"] != 1:
            raise ValueError("glm5_d950pr validation is prefill-only: output_tokens must be 1")
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
        demand = r.get("demand_Bps")
        if demand is not None and (not math.isfinite(demand) or demand <= 0):
            raise ValueError("demand_Bps must be positive and finite")


async def simulate(c, rows, output_dir):
    c = normalize_config(c)
    validate(rows, c)
    rows = sorted(rows, key=lambda r: (r["arrival_s"], r["id"]))
    by_id = {r["id"]: r for r in rows}
    store = Storage(c["storage"], c["block_size"])
    engines = [Engine(i, c, store) for i in range(c["workers"])]
    profiles = {r["id"]: build_profile(r, c) for r in rows}
    if c["compute"].get("mode", "linear") == "linear":
        derived_demand = c["storage"]["kv_bytes_per_token"] / c["compute"]["prefill_token_s"]
        demands = {r["id"]: r.get("demand_Bps", derived_demand) for r in rows}
    else:
        demands = {r["id"]: r.get("demand_Bps", max(1.0, profiles[r["id"]].pressure_Bps))
                   for r in rows}
    for e in engines:
        e.scheduler.connector.demands = demands
    reqs = {r["id"]: make_request({**r,'deadline_s':profiles[r['id']].deadline_s},
                                c["block_size"], c["policy"]) for r in rows}
    for r in rows:
        store.present.update(reqs[r["id"]].block_hashes[:r.get("remote_prefix_tokens", 0) // c["block_size"]])
    admission = AdmissionController(c["admission"])
    router = Router()
    stats = {r["id"]: {"arrival_s": r["arrival_s"], "output_tokens": 0} for r in rows}
    events, feedback = [], []
    now = rows[0]["arrival_s"]
    store.now = now
    cursor = completed = event_seq = 0
    peak_held = peak_loads = peak_blocks = 0
    over_cap_s = 0.0

    def drain_logs():
        events.extend(admission.logs)
        admission.logs.clear()

    def harvest(e):
        nonlocal event_seq
        for event in e.control_events:
            event_seq += 1
            event = replace(event, visible_s=event.occurred_s + c["admission"]["feedback_delay_s"])
            heapq.heappush(feedback, (event.visible_s, event_seq, event))
            events.append({"t": event.occurred_s, "event": event.kind,
                           "id": event.request_id, "worker": event.worker_id,
                           "event_id": event.event_id, "occurred_s": event.occurred_s,
                           "visible_s": event.visible_s, **event.payload})
        e.control_events.clear()
        events.extend(e.step_events)
        e.step_events.clear()

    def deliver():
        count = 0
        while feedback and feedback[0][0] <= now:
            _, _, event = heapq.heappop(feedback)
            admission.on_event(event, now)
            events.append({"t": now, "event": "control_event_delivered",
                           "id": event.request_id, "event_id": event.event_id,
                           "kind": event.kind, "visible_s": event.visible_s})
            count += 1
        drain_logs()
        return count

    async def consume(e, scheduled):
        nonlocal completed
        step_id = e.steps if scheduled[0].num_scheduled_tokens else None
        for entry in e.complete(scheduled):
            rid = entry.request_id
            s = stats[rid]
            if entry.new_token_ids and "first_token_s" not in s:
                s["first_token_s"] = now
                e.emit_control("first_token", rid, now)
                await router.first_token(rid)
            s["output_tokens"] += len(entry.new_token_ids)
            if entry.finish_reason is not None:
                s["finish_s"] = now
                s["preemptions"] = reqs[rid].num_preemptions
                completed += 1
                e.emit_control("request_finished", rid, now)
                await router.finish(rid)
        events.append({"t": now, "event": "step_complete", "worker": e.id,
                       "step_id": step_id, "scheduled": dict(scheduled[0].num_scheduled_tokens),
                       "waiting_remote": [r.request_id for r in e.scheduler.requests.values()
                                          if r.status.name == "WAITING_FOR_REMOTE_KVS"]})
        harvest(e)

    try:
        await router.start(c)
        iterations = 0
        while completed < len(rows):
            iterations += 1
            if iterations > 1000000:
                raise RuntimeError(f"event-loop guard at {now}; {admission.snapshot(now)}")
            deliver()
            arrivals = []
            while cursor < len(rows) and rows[cursor]["arrival_s"] <= now:
                arrivals.append(profiles[rows[cursor]["id"]])
                cursor += 1
            admission.on_arrivals(arrivals, now)
            drain_logs()
            # 只在新反馈确实使中央队列可准入时重复同刻调度，不对空 Scheduler 忙等。
            for same_time_round in range(10000):
                while (decision := admission.pick(now)) is not None:
                    admission.reserve(decision, now)
                    rid = decision.request_id
                    wid = await router.route(by_id[rid])
                    engines[wid].scheduler.add_request(reqs[rid])
                    admission.mark_submitted(rid, wid, now)
                    stats[rid].update(worker=wid, submitted_s=now)
                    # 兼容旧分析工具；新语义由 request_arrived/request_submitted 明确区分。
                    events.append({"t": now, "event": "arrival", "id": rid, "worker": wid,
                                   "meaning": "legacy_submission_alias"})
                    peak_held = max(peak_held, admission.held)
                drain_logs()
                for e in engines:
                    immediate = e.schedule(now)
                    harvest(e)
                    if immediate is not None:
                        await consume(e, immediate)
                        second = e.schedule(now)
                        harvest(e)
                        if second is not None:
                            await consume(e, second)
                peak_held = max(peak_held, admission.held)
                peak_loads = max(peak_loads, len(store.pending))
                peak_blocks = max(peak_blocks, sum(store.pending.values()))
                delivered = deliver()
                if not (delivered and admission.pending and admission.pick(now) is not None):
                    break
            else:
                raise RuntimeError(f"same-time guard at {now}; {admission.snapshot(now)}")
            if completed == len(rows):
                break
            wake = admission.next_wakeup(now)
            target = min([rows[cursor]["arrival_s"] if cursor < len(rows) else math.inf,
                          store.next_time(), feedback[0][0] if feedback else math.inf,
                          wake if wake is not None else math.inf]
                         + [e.inflight[2] for e in engines if e.inflight])
            if not math.isfinite(target) or target <= now:
                raise RuntimeError(f"deadlock/nonprogress at {now}; {admission.snapshot(now)}")
            dt = target - now
            for e in engines:
                e.account(dt)
            if admission.cap is not None and admission.held > admission.cap:
                over_cap_s += dt
            events.append({"t": target, "event": "admission_interval", "from": now,
                           **asdict(admission.snapshot(now)),
                           "queued": len(admission.pending), "lease_count": sum(r.lease_open for r in admission.records.values()),
                           "outstanding_read_requests": len(store.pending),
                           "outstanding_read_blocks": sum(store.pending.values()),
                           "tfs_pressure_sum_Bps": sum(r.profile.pressure_Bps for r in admission.records.values() if r.lease_open)})
            if c.get("storage_interval_log"):
                demand, actual = store.link_flux()
                if demand > 0:
                    events.append({"t": target, "event": "storage_interval", "from": now,
                                   "demand_Bps": demand, "actual_Bps": actual,
                                   "queued_reads": sum(len(q) for q in store.queues.values()),
                                   "bytes_remaining": sum(r.remaining for q in store.queues.values() for r in q)})
            recvs = store.advance(target)
            now = target
            for wid, rid in recvs:
                engines[wid].pending_recvs.add(rid)
                engines[wid].load_completed(rid, now)
                harvest(engines[wid])
                events.append({"t": now, "event": "kv_received", "worker": wid, "id": rid})
            for e in engines:
                if e.inflight and e.inflight[2] <= now:
                    await consume(e, e.inflight)
        business_end = max(s["finish_s"] for s in stats.values())
        # 收尾只排空观测，不延长业务 makespan 或 Worker 时间积分。
        for e in engines:
            immediate = e.schedule(now)
            harvest(e)
            if immediate:
                await consume(e, immediate)
        while feedback:
            now = max(now, feedback[0][0])
            deliver()
        ttfts = []
        for rid, s in stats.items():
            rec = admission.records[rid]
            s.update({k: v for k, v in asdict(rec).items() if k != "profile"})
            s.update(asdict(rec.profile))
            s["ttft_s"] = s["first_token_s"] - s["arrival_s"]
            s["central_wait_s"] = s["submitted_s"] - s["arrival_s"]
            s["engine_ttft_s"] = s["first_token_s"] - s["submitted_s"]
            s["load_submit_wait_s"] = s["load_submit_s"] - s["submitted_s"] if s["load_submit_s"] is not None else None
            s["load_duration_s"] = s["load_done_s"] - s["load_submit_s"] if s["load_done_s"] is not None else None
            s["external_tokens"] = engines[s["worker"]].scheduler.connector.external_tokens.get(rid, 0)
            ttfts.append(s["ttft_s"])
            assert s["output_tokens"] == by_id[rid]["output_tokens"]
        span = business_end - rows[0]["arrival_s"]
        source = Path(inspect.getfile(type(engines[0].scheduler)))
        invalid = sorted(admission.invalid_reasons | ({"preemption"} if any(s["preemptions"] for s in stats.values()) else set()))
        report = {
            "implementation_sha256": hashlib.sha256(b"".join(p.name.encode()+p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(),
            "report_schema_version": 2, "status": "completed",
            "scope": "request admission + real Dynamo cache-blind selector + real scheduler; synthetic timing",
            "versions": {p: importlib.metadata.version(p) for p in ("ai-dynamo-runtime", "vllm", "torch")},
            "source_pins": json.loads((Path(__file__).resolve().parents[1] / "versions.json").read_text()),
            "workload_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "scheduler_file": str(source), "scheduler_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "config": c, "requests_completed": completed, "makespan_s": span,
            "business_end_s": business_end, "feedback_drained_s": now,
            "throughput_req_s": completed / span, "mean_ttft_s": statistics.mean(ttfts),
            "p95_ttft_s": sorted(ttfts)[math.ceil(0.95 * len(ttfts)) - 1],
            "ttft_slo_attainment": sum(t <= c["ttft_slo_s"] for t in ttfts) / len(ttfts),
            "profile_deadline_attainment": sum(s["first_token_s"] <= s["deadline_s"] for s in stats.values()) / len(stats),
            "profile_goodput_req_s": sum(s["first_token_s"] <= s["deadline_s"] for s in stats.values()) / span,
            "fixed_goodput_req_s": sum(t <= c["ttft_slo_s"] for t in ttfts) / span,
            "bytes_read": round(store.bytes_transferred),
            "estimated_read_bytes": sum(s["estimated_read_bytes"] for s in stats.values()),
            "submitted_read_bytes": sum(s["submitted_read_bytes"] for s in stats.values()),
            "workers": [{"id": e.id, "compute_s": e.compute_s, "stall_s": e.stall_s,
                         "idle_s": e.idle_s, "steps": e.steps, "zero_token_steps": e.zero_token_steps,
                         "unfinished": e.scheduler.get_num_unfinished_requests(),
                         "free_blocks": e.scheduler.kv_cache_manager.block_pool.free_block_queue.num_free_blocks}
                        for e in engines],
            "requests": stats, "router_loads_after_finish": router.service.loads(),
            "admission_final": {"pending": len(admission.pending),
                                "open_leases": sum(r.lease_open for r in admission.records.values()),
                                "undelivered_events": len(feedback)},
            "admission_summary": {"peak_heavy_held": peak_held, "aging_exemptions": admission.exemptions,
                                  "over_cap_s": over_cap_s, "zero_quantile_rule": "minimum_positive_if_quantile_zero"},
            "read_concurrency": {"peak_requests": peak_loads, "peak_blocks": peak_blocks},
            "main_experiment_valid": not invalid, "out_of_scope_reasons": invalid,
            "simulated_compute_busy_fraction": sum(e.compute_s for e in engines) / (len(engines) * span),
            "limitations": ["No numerical device execution or full EngineCore loop",
                            "Dynamo KV-event bridge disabled", "Native equal-cost routing tie can be random",
                            "No layerwise overlap, PD, writeback, cancellation or load failures",
                            "Fixed reference bandwidth; admission is not real-time storage sensing",
                            "Analytic compute, not measured hardware time; stall_s includes all unfinished noncomputing time"],
            **summarize(stats, events, span, c["ttft_slo_s"]),
        }
        assert not store.pending and completed == len(rows)
        assert all(e.scheduler.get_num_unfinished_requests() == 0 for e in engines)
        assert report["admission_final"] == {"pending": 0, "open_leases": 0, "undelivered_events": 0}
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        (out / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events))
        print(json.dumps({k: report[k] for k in ("status", "requests_completed", "makespan_s", "mean_ttft_s", "bytes_read")}, indent=2))
        return report
    finally:
        await router.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/demo.json")
    p.add_argument("--trace", default="configs/demo.jsonl")
    p.add_argument("--output", default="results/demo")
    p.add_argument("--scheduler", choices=["upstream", "ascend_default", "ascend_balance"])
    p.add_argument("--policy", choices=["fcfs", "priority"])
    p.add_argument("--bandwidth-policy", choices=["active_split", "max_min_fair"])
    p.add_argument("--admission", choices=["immediate", "slack_only", "quota_fcfs", "tfs", "tfs_no_aging"])
    p.add_argument("--release-on", choices=["first_token", "load_done"])
    p.add_argument("--max-num-seqs", type=int)
    p.add_argument("--max-num-batched-tokens", type=int)
    a = p.parse_args()
    c = json.loads(Path(a.config).read_text())
    for key in ("scheduler", "policy", "max_num_seqs", "max_num_batched_tokens"):
        if getattr(a, key) is not None:
            c[key] = getattr(a, key)
    if a.policy is not None:
        c['request_ordering'] = 'edf' if a.policy == 'priority' else 'fcfs'
    for key, value in (("mode", a.admission), ("release_on", a.release_on)):
        if value is not None:
            c.setdefault("admission", {})[key] = value
    if a.bandwidth_policy:
        c["storage"]["bandwidth_policy"] = a.bandwidth_policy
    rows = [json.loads(x) for x in Path(a.trace).read_text().splitlines() if x.strip()]
    asyncio.run(simulate(c, rows, a.output))


if __name__ == "__main__":
    main()
