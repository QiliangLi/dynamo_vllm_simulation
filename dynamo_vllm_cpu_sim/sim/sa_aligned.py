"""SA 输入/画像/存储对齐；仅路由、实例排队和每步组批交给真实 Dynamo/vLLM。"""

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import statistics

os.environ["VLLM_PLUGINS"] = ""
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("VLLM_LOGGING_LEVEL", "ERROR")
os.environ.setdefault("VLLM_ALLOW_LONG_MAX_MODEL_LEN", "1")

from sim.admission import AdmissionController
from sim.control_events import ControlEvent
from sim.request_profile import RequestProfile
from sim.priority import normalize_ordering
from sim.router import Router
from sim.sa_model import LayerStorage, reference
from sim.sa_layer_engine import SALayerEngine, make_sa_request
from sim.tfs_metrics import summarize


def implementation_sha():
    return hashlib.sha256(b"".join(p.name.encode()+p.read_bytes()
                                  for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()


from sim.sa_admission import SAAdmission


def materialize(rows):
    """原始 h/u 不变，仅提供真实调度 API 所需的合成 token IDs。"""
    return [{**r, "prompt_token_ids": [i//31999+1, i%31999+1]
             + [3]*(r["h_tokens"]+r["u_tokens"]-2),
             "remote_prefix_tokens": r["h_tokens"], "output_tokens": 1}
            for i, r in enumerate(rows)]


async def simulate(config, rows, output):
    c = deepcopy(config)
    normalize_ordering(c)
    if (c["block_size"] != 1 or c["enable_prefix_caching"]
            or c["scheduler"] != "upstream" or c["storage"]["bandwidth_policy"] != "sa_max_min"
            or c["admission"]["aging_enabled"] or c["admission"]["feedback_delay_s"] != 0):
        raise ValueError("SA alignment contract violated")
    inputs = sorted(rows, key=lambda r: (r["arrival_s"], r["id"]))
    for row in inputs:
        if row["h_tokens"]+row["u_tokens"]+1 > c["max_model_len"] or row["u_tokens"] > c["max_num_batched_tokens"]:
            raise ValueError("unaligned chunking/length; singleton must fit")
        _, _, t0 = reference(row, c["sa_profile"], c["storage"]["shared_link_Bps"], c["storage"]["q_max_Bps"])
        if abs(t0-row["T0_s"]) > 1e-10 or abs(row["arrival_s"]+4*t0-row["deadline_s"]) > 1e-10:
            raise ValueError("SA reference profile/deadline mismatch")
    rows = materialize(inputs)
    byid = {r["id"]: r for r in rows}
    store = LayerStorage(c["storage"]["shared_link_Bps"], c["storage"]["q_max_Bps"])
    engines = [SALayerEngine(i, c, store, rows) for i in range(c["workers"])]
    reqs = {r["id"]: make_sa_request(r,c.get('policy','fcfs')) for r in rows}
    ctrl, router = SAAdmission(c["admission"]), Router(c.get("router_mode", "kv"))
    stats = {r["id"]: {"arrival_s": r["arrival_s"], "class_id": r["class_id"],
                       "h_tokens": r["h_tokens"], "u_tokens": r["u_tokens"],
                       "load_submit_s": None, "load_done_s": None,
                       "submitted_read_bytes": 0, "load_attempts": 0, "external_tokens": 0}
             for r in rows}
    events, seq = [], 0
    now = store.now = 0.0
    cursor = done = 0

    def deliver(kind, rid, wid, payload=None):
        nonlocal seq
        seq += 1
        event = ControlEvent(f"sa:{seq}", kind, rid, wid, now, now, payload or {})
        ctrl.on_event(event, now)
        events.append({"t": now, "event": kind, "id": rid, "worker": wid,
                       "event_id": event.event_id, **(payload or {})})

    def harvest():
        events.extend(ctrl.logs)
        ctrl.logs.clear()
        events.extend(store.events)
        store.events.clear()
        for e in engines:
            for ev in e.output_events:
                events.append(ev)
                if ev["event"] == "load_submitted":
                    rid = ev["id"]
                    s = stats[rid]
                    s["load_submit_s"] = now
                    s["load_attempts"] += 1
                    s["submitted_read_bytes"] += ev["submitted_read_bytes"]
                    # 与旧控制事件共用语义，事件中仍只登记一次真实提交。
                    load_event = ControlEvent(f"sa-load:{rid}:{s['load_attempts']}",
                           "load_submitted", rid, e.id, now, now,
                           {"attempt_id": s["load_attempts"], "submitted_read_bytes": ev["submitted_read_bytes"]})
                    ctrl.on_event(load_event, now)
            e.output_events.clear()

    async def complete(e, scheduled):
        nonlocal done
        for entry in e.complete(scheduled):
            rid = entry.request_id
            s = stats[rid]
            if entry.new_token_ids:
                if "first_token_s" in s:
                    raise RuntimeError("SA experiment emitted more than one token")
                s["first_token_s"] = now
                deliver("first_token", rid, e.id)
                await router.first_token(rid)
            if entry.finish_reason is not None:
                s["finish_s"] = now
                s["preemptions"] = reqs[rid].num_preemptions
                s["external_tokens"] = e.scheduler.connector.external_tokens.get(rid, 0)
                deliver("request_finished", rid, e.id)
                await router.finish(rid)
                done += 1
        events.append({"t": now, "event": "step_complete", "worker": e.id,
                       "step_id": e.steps, "scheduled": dict(scheduled[0].num_scheduled_tokens)})

    try:
        await router.start(c)
        for iteration in range(100000):
            # 完成旧 step 后先处理到达，再准入与真实组批。
            for e in engines:
                result = e.close_layers(now)
                if result is not None:
                    await complete(e, result)
            arrived = []
            while cursor < len(rows) and rows[cursor]["arrival_s"] <= now:
                r = rows[cursor]
                v, comp, t0 = reference(r, c["sa_profile"], store.bandwidth, store.qmax)
                arrived.append(RequestProfile(r["id"], r["arrival_s"], r["class_id"],
                           r["h_tokens"]+r["u_tokens"], r["h_tokens"], v*c["sa_profile"]["L"],
                           comp*c["sa_profile"]["L"], v/comp, t0, r["deadline_s"]))
                cursor += 1
            ctrl.on_arrivals(arrived, now)
            while (decision := ctrl.pick(now)) is not None:
                ctrl.reserve(decision, now)
                rid = decision.request_id
                wid = await router.route(byid[rid])
                engines[wid].scheduler.add_request(reqs[rid])
                ctrl.mark_submitted(rid, wid, now)
                stats[rid].update(worker=wid, submitted_s=now)
            for e in engines:
                e.schedule_layer_step(now)
                # 零读层可在同刻开始，非零读必须等待 layer_read_complete。
                e.close_layers(now)
            harvest()
            store.allocate()
            if done == len(rows):
                break
            target = min([rows[cursor]["arrival_s"] if cursor < len(rows) else math.inf,
                          store.next_time()]
                         + [e.runtime.compute_end for e in engines if e.runtime])
            if not math.isfinite(target) or target <= now:
                raise RuntimeError(f"SA aligned deadlock at {now}")
            for e in engines:
                e.account(target-now)
            events.append({"t": target, "event": "admission_interval", "from": now,
                           "queued": len(ctrl.pending), "heavy_held": ctrl.held,
                           "heavy_cap": ctrl.cap, "heavy_threshold_Bps": ctrl.theta})
            reads = store.advance(target)
            now = target
            for wid, stepid, layer in reads:
                e = engines[wid]
                if e.runtime is None or e.runtime.step_id != stepid:
                    raise RuntimeError("orphan layer read")
                e.runtime.ready[layer] = True
                if layer == c["sa_profile"]["L"]-1:
                    for rid in e.runtime.remote:
                        stats[rid]["load_done_s"] = now
                        deliver("load_completed", rid, wid, {"attempt_id": stats[rid]["load_attempts"]})
        else:
            raise RuntimeError("aligned event budget exhausted")
        harvest()
        for rid, s in stats.items():
            rec = ctrl.records[rid]
            s.update(asdict(rec.profile))
            s.update(lease_open=rec.lease_open, state=rec.state,
                     ttft_s=s["first_token_s"]-s["arrival_s"],
                     central_wait_s=s["submitted_s"]-s["arrival_s"],
                     engine_ttft_s=s["first_token_s"]-s["submitted_s"],
                     load_submit_wait_s=s["load_submit_s"]-s["submitted_s"] if s["load_submit_s"] is not None else None,
                     load_duration_s=s["load_done_s"]-s["load_submit_s"] if s["load_done_s"] is not None else None)
        invalid = sorted(ctrl.invalid_reasons | ({"preemption"} if any(s["preemptions"] for s in stats.values()) else set()))
        source = Path(inspect.getfile(type(engines[0].scheduler)))
        report = {"report_schema_version": 3, "status": "completed", "config": c,
                  "scope": "SA E26f profile + layerwise overlap + aggregate max_min; native Dynamo reservations/vLLM batch",
                  "router_mode": router.mode,
                  "router_selection": ("Python cyclic worker selection + native create_reservation"
                                       if router.mode == "round-robin" else "native select_and_reserve"),
                  "implementation_sha256": implementation_sha(),
                  "workload_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
                  "scheduler_file": str(source), "scheduler_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "requests_completed": done, "business_end_s": now, "makespan_s": now,
                  "mean_ttft_s": statistics.mean(s["ttft_s"] for s in stats.values()),
                  "profile_deadline_attainment": sum(s["first_token_s"] <= s["deadline_s"] for s in stats.values())/done,
                  "bytes_read": store.bytes_transferred, "submitted_read_bytes": store.bytes_submitted,
                  "read_concurrency": {"peak_flows": store.peak_flows},
                  "main_experiment_valid": not invalid, "out_of_scope_reasons": invalid,
                  "requests": stats, "router_loads_after_finish": router.service.loads(),
                  "workers": [{"id": e.id, "compute_s": e.compute_s, "stall_s": e.stall_s,
                               "idle_s": e.idle_s, "steps": e.steps,
                               "peak_kv_blocks": e.peak_kv_blocks, "peak_memory_bytes": e.peak_memory_bytes,
                               "unfinished": e.scheduler.get_num_unfinished_requests(),
                               "free_blocks": e.scheduler.kv_cache_manager.block_pool.free_block_queue.num_free_blocks}
                              for e in engines],
                  "admission_final": {"pending": len(ctrl.pending), "open_leases": sum(r.lease_open for r in ctrl.records.values()),
                                      "undelivered_events": 0},
                  "limitations": ["Synthetic SA execution profile; no numerical GPU execution",
                                   "Real Scheduler and KV manager, no full EngineCore/ModelRunner",
                                   ("Python round-robin policy adapter; not the Rust HTTP frontend"
                                    if router.mode == "round-robin" else "Dynamo native equal-cost routing ties may vary"),
                                   "Dynamo routing/worker queues and vLLM batch/chunk choices replace SA dispatch",
                                   "Logical block=1 metadata only; no claim of device block=1 support"],
                  **summarize(stats, events, now, c["ttft_slo_s"])}
        if store.flows or invalid or any(e.runtime is not None for e in engines):
            raise RuntimeError(f"alignment invalid: {invalid}")
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        (output/"report.json").write_text(json.dumps(report, indent=2)+"\n")
        (output/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
        print(json.dumps({k:report[k] for k in ("requests_completed", "makespan_s", "mean_ttft_s", "profile_deadline_attainment", "bytes_read")}))
        return report
    finally:
        await router.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--trace", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    asyncio.run(simulate(json.loads(Path(a.config).read_text()), json.loads(Path(a.trace).read_text()), a.output))


if __name__ == "__main__":
    main()
