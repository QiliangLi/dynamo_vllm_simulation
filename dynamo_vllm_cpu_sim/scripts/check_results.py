#!/usr/bin/env python3
"""Integration invariants: real request lifecycle, remote waits, and reservations."""

import json
from pathlib import Path
import sys

for name in sys.argv[1:]:
    directory = Path(name)
    r = json.loads((directory / "report.json").read_text())
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
    ]
    assert r["status"] == "completed"
    assert r["requests_completed"] == len(r["requests"])
    assert all(w["unfinished"] == 0 for w in r["workers"])
    # The null sentinel consumes one block. Cached but unreferenced blocks are reclaimable.
    assert all(w["free_blocks"] == r["config"]["num_blocks"] - 1 for w in r["workers"])
    for model in r["router_loads_after_finish"]:
        assert model["pending_count"] == 0
        for worker in model["loads"]:
            for key in (
                "active_requests",
                "potential_prefill_tokens",
                "potential_decode_blocks",
            ):
                assert worker[key] == 0, (key, worker)
    waiting = {
        rid
        for e in events
        if e["event"] == "step_complete"
        for rid in e["waiting_remote"]
    }
    received = {e["id"]: e["t"] for e in events if e["event"] == "kv_received"}
    if r["bytes_read"]:
        assert waiting and received
    for rid, s in r["requests"].items():
        assert s["arrival_s"] <= s["first_token_s"] <= s["finish_s"]
        if s["external_tokens"]:
            assert rid in waiting and rid in received
            assert received[rid] <= s["first_token_s"]
    if r.get("report_schema_version", 1) >= 2:
        assert r["admission_final"] == {"pending": 0, "open_leases": 0, "undelivered_events": 0}
        arrived = [e["id"] for e in events if e["event"] == "request_arrived"]
        submitted = [e["id"] for e in events if e["event"] == "request_submitted"]
        assert sorted(arrived) == sorted(submitted) == sorted(r["requests"])
        assert len(arrived) == len(set(arrived))
        released = [e["id"] for e in events if e["event"] == "admission_released"]
        assert sorted(released) == sorted(arrived)
        actual_events = {e["event_id"]: e for e in events if "event_id" in e and e["event"] != "control_event_delivered"}
        delivered = [e for e in events if e["event"] == "control_event_delivered"]
        assert len(delivered) == len(actual_events)
        for e in delivered:
            assert e["t"] + 1e-12 >= actual_events[e["event_id"]]["visible_s"]
        starts = {(e["worker"], e["step_id"]): e for e in events if e["event"] == "step_scheduled"}
        ends = {(e["worker"], e["step_id"]): e for e in events if e["event"] == "step_complete" and e.get("step_id") is not None}
        assert starts.keys() == ends.keys()
        for key, e in starts.items():
            assert e["num_requests"] == len(e["num_scheduled_tokens"]) > 0
            assert abs(e["t"] + e["duration_s"] - ends[key]["t"]) < 1e-9
            assert sum(e["num_scheduled_tokens"].values()) == e["prefill_tokens"] + e["decode_tokens"]
        for e in events:
            if e["event"] == "admission_reserved" and e["mode"] in ("tfs", "quota_fcfs") and e["reason"] == "normal" and e["heavy"]:
                assert e["held_after"] <= e["cap"]
            if e["event"] == "no_external_load":
                assert not any(x["event"] == "load_submitted" and x["id"] == e["id"] for x in events)
                assert any(e["id"] in x["num_scheduled_tokens"] and abs(e["t"] - x["t"]) < 1e-12 for x in starts.values())
        assert abs(r["bytes_read"] - r["submitted_read_bytes"]) <= 1
        for w in r["workers"]:
            assert abs(w["compute_s"] + w["stall_s"] + w["idle_s"] - r["makespan_s"]) < 1e-9
        for rid, s in r["requests"].items():
            assert s["arrival_s"] <= s["submitted_s"] <= s["first_token_s"] <= s["finish_s"]
            assert not s["lease_open"] and s["state"] == "FINISHED"
            assert abs(s["ttft_s"] - s["central_wait_s"] - s["engine_ttft_s"]) < 1e-9
            if s["no_external_load"]:
                assert s["load_submit_wait_s"] is None and not s["load_attempts"]
            release = next(e for e in events if e["event"] == "admission_released" and e["id"] == rid)
            if r["config"]["admission"]["release_on"] == "first_token":
                assert release["t"] + 1e-12 >= s["first_token_s"] + r["config"]["admission"]["feedback_delay_s"]
    print(
        f"{directory}: lifecycle / KV wait / block reclamation / native reservations PASS"
    )
