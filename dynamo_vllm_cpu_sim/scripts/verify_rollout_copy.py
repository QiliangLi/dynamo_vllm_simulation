"""Verify that the simulated world (real vLLM Scheduler objects + Storage) can be
deep-copied mid-flight and that a copy driven by the same action sequence
reproduces the original trajectory bit-for-bit.

This is the feasibility precondition for using the simulation kernel itself as
the MPC rollout/prediction model: if a deep copy diverges from the original
under identical driving, per-candidate branch-and-roll prediction would be
unsound. Run from the package root:

    .venv/bin/python scripts/verify_rollout_copy.py
"""

import copy
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.engine import Engine, make_request
from sim.storage import Storage

c = json.loads((ROOT / "configs/demo.json").read_text())
rows = [
    json.loads(x)
    for x in (ROOT / "configs/demo.jsonl").read_text().splitlines()
    if x.strip()
]
rows.sort(key=lambda r: (r["arrival_s"], r["id"]))


def build_world():
    store = Storage(c["storage"], c["block_size"])
    engines = [Engine(i, c, store) for i in range(c["workers"])]
    derived = c["storage"]["kv_bytes_per_token"] / c["compute"]["prefill_token_s"]
    for e in engines:
        e.scheduler.connector.demands = {r["id"]: derived for r in rows}
    reqs = {r["id"]: make_request(r, c["block_size"], c["policy"]) for r in rows}
    for r in rows:
        store.present.update(
            reqs[r["id"]].block_hashes[
                : r.get("remote_prefix_tokens", 0) // c["block_size"]
            ]
        )
    return store, engines, reqs


def fingerprint(engines, store):
    """Comparable state across worlds: clocks, queues, per-engine scheduler
    queues, chunk progress, KV blocks, inflight step."""
    fp = {
        "t": round(store.now, 12),
        "bytes": round(store.bytes_transferred, 6),
        "pending": sorted(str(k) for k in store.pending),
        "queues": {
            f"{d}:{p}": [round(r.remaining, 6) for r in q]
            for (d, p), q in sorted(store.queues.items())
            if q
        },
    }
    for e in engines:
        s = e.scheduler
        fp[f"w{e.id}"] = {
            "waiting": [r.request_id for r in s.waiting],
            "running": [r.request_id for r in s.running],
            "remote": sorted(
                r.request_id
                for r in s.requests.values()
                if r.status.name == "WAITING_FOR_REMOTE_KVS"
            ),
            "computed": {r.request_id: r.num_computed_tokens for r in s.running},
            "free_blocks": s.kv_cache_manager.block_pool.free_block_queue.num_free_blocks,
            "inflight": (
                None
                if e.inflight is None
                else [dict(e.inflight[0].num_scheduled_tokens), e.inflight[2]]
            ),
            "steps": e.steps,
        }
    return fp


def drive_one_step(engines, store, now):
    """One round of a glue-layer loop; the identical function drives both
    worlds. Mirrors run.py semantics: empty batches complete immediately
    (that is how finished_recving reaches the scheduler), inflight steps
    complete when their virtual finish time is reached."""
    trace = []
    for e in engines:
        immediate = e.schedule(now)
        if immediate is not None:
            for entry in e.complete(immediate):
                if entry.finish_reason is not None:
                    trace.append(("finish", e.id, entry.request_id))
            trace.append(("sched", e.id, dict(immediate[0].num_scheduled_tokens)))
            second = e.schedule(now)
            if second is not None:
                for entry in e.complete(second):
                    if entry.finish_reason is not None:
                        trace.append(("finish2", e.id, entry.request_id))
                trace.append(("sched2", e.id, dict(second[0].num_scheduled_tokens)))
    target = min([store.next_time()] + [e.inflight[2] for e in engines if e.inflight])
    if not math.isfinite(target) or target <= now:
        return trace, now, True
    for e in engines:
        e.account(target - now)
    recvs = store.advance(target)
    now = target
    for wid, rid in recvs:
        engines[wid].pending_recvs.add(rid)
        trace.append(("recv", wid, rid))
    for e in engines:
        if e.inflight and e.inflight[2] <= now + 1e-12:
            for entry in e.complete(e.inflight):
                if entry.finish_reason is not None:
                    trace.append(("finish", e.id, entry.request_id))
    return trace, now, False


def world_done(engines, store):
    return (
        all(
            e.scheduler.get_num_unfinished_requests() == 0 and e.inflight is None
            for e in engines
        )
        and not store.pending
    )


def main():
    # Phase 1: run the original world greedily for a bounded number of rounds,
    # injecting arrivals round-robin, so the split point has live state
    # (requests mid-flight, storage transferring).
    store, engines, reqs = build_world()
    rows_pending = list(rows)
    now = 0.0
    split_after = 30
    steps = 0
    while steps < split_after:
        while rows_pending and rows_pending[0]["arrival_s"] <= now + 1e-12:
            r = rows_pending.pop(0)
            engines[steps % c["workers"]].scheduler.add_request(reqs[r["id"]])
        if world_done(engines, store):
            break
        _, now, stalled = drive_one_step(engines, store, now)
        if stalled:
            if rows_pending and rows_pending[0]["arrival_s"] > now:
                now = rows_pending[0]["arrival_s"]
                store.now = now
            else:
                break
        steps += 1
    fp_split = fingerprint(engines, store)
    unfinished = sum(e.scheduler.get_num_unfinished_requests() for e in engines)
    assert unfinished > 0, "split point has no live state; test is vacuous"
    print(f"[split] rounds={steps} t={now:.6f} unfinished={unfinished}")

    # Phase 2: deep-copy the entire world mid-flight.
    store2, engines2, _reqs2 = copy.deepcopy((store, engines, reqs))
    assert fingerprint(engines2, store2) == fp_split, "copy differs at split point"
    print("[copy ] deepcopy of (Storage, Engines-with-real-Scheduler, requests) OK")

    # Phase 3: drive both worlds with the identical future action sequence and
    # compare the full fingerprint after every round.
    mismatch = None
    rounds = 0
    last_progress = None
    while not world_done(engines, store) and rounds < 2000:
        _, now1, _ = drive_one_step(engines, store, now)
        _, now2, _ = drive_one_step(engines2, store2, now)
        now = now1
        rounds += 1
        fp1, fp2 = fingerprint(engines, store), fingerprint(engines2, store2)
        if fp1 != fp2:
            mismatch = (rounds, fp1, fp2)
            break
        progress = (fp1["t"], tuple(fp1[f"w{i}"]["steps"] for i in range(c["workers"])))
        if progress == last_progress:
            break  # no further event can fire; stop comparing
        last_progress = progress

    if mismatch:
        i, fp1, fp2 = mismatch
        print(f"[FAIL ] worlds diverged at future round {i}")
        for k in fp1:
            if fp1[k] != fp2.get(k):
                print(f"  key={k}\n  world1={fp1[k]}\n  world2={fp2.get(k)}")
        sys.exit(1)

    print(
        f"[PASS ] copy-driven world reproduced original bit-for-bit over {rounds} "
        f"future rounds (t={now:.6f}, bytes={store.bytes_transferred:.3f}, "
        f"steps={[e.steps for e in engines]})"
    )
    print("[PASS ] rollout-by-deepcopy is a usable MPC prediction model here")


if __name__ == "__main__":
    main()
