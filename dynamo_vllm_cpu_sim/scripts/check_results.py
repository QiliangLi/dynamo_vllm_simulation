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
    print(
        f"{directory}: lifecycle / KV wait / block reclamation / native reservations PASS"
    )
