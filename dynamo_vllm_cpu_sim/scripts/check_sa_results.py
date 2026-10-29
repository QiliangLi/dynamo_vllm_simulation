#!/usr/bin/env python3
"""对 SA 对齐实验检查输入、因果、层流、资源、真实引擎和路由守恒。"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.sa_model import layer_compute, reference


def check_profile(row, stats, config):
    storage = config["storage"]
    v, c, t0 = reference(row, config["sa_profile"],
                         storage["shared_link_Bps"], storage["q_max_Bps"])
    assert abs(t0 - row["T0_s"]) < 1e-10
    assert abs(stats["pressure_Bps"] - v / c) < 0.001


def check(directory):
    directory = Path(directory)
    r = json.loads((directory / "report.json").read_text())
    events = [
        json.loads(s) for s in (directory / "events.jsonl").read_text().splitlines()
    ]
    trace = json.loads(Path(r["config"]["source_trace"]).read_text())
    assert (
        r["workload_sha256"]
        == hashlib.sha256(
            json.dumps(
                sorted(trace, key=lambda s: (s["arrival_s"], s["id"])), sort_keys=True
            ).encode()
        ).hexdigest()
    )
    assert r["status"] == "completed" and r["main_experiment_valid"]
    assert r["requests_completed"] == len(trace) == len(r["requests"])
    if r["config"].get("router_mode", "kv") == "round-robin":
        dispatches = [e for e in events if e["event"] == "request_submitted"]
        assert len(dispatches) == len(trace)
        assert [e["worker"] for e in dispatches] == [
            i % r["config"]["workers"] for i in range(len(trace))
        ]
        assert all(r["requests"][e["id"]]["worker"] == e["worker"] for e in dispatches)
    assert r["admission_final"] == {
        "pending": 0,
        "open_leases": 0,
        "undelivered_events": 0,
    }
    for model in r["router_loads_after_finish"]:
        assert model["pending_count"] == 0
        for w in model["loads"]:
            assert all(
                w[k] == 0
                for k in (
                    "active_requests",
                    "potential_prefill_tokens",
                    "potential_decode_blocks",
                )
            )
    for w in r["workers"]:
        assert (
            w["unfinished"] == 0 and w["free_blocks"] == r["config"]["num_blocks"] - 1
        )
        assert abs(w["compute_s"] + w["stall_s"] + w["idle_s"] - r["makespan_s"]) < 1e-8
        assert w["peak_memory_bytes"] <= r["config"]["memory"]["total_bytes"]
    steps = {
        (e["worker"], e["step_id"]): e for e in events if e["event"] == "step_started"
    }
    completed = {
        (e["worker"], e["step_id"]): e for e in events if e["event"] == "step_complete"
    }
    assert steps.keys() == completed.keys()
    read_starts = {
        tuple(e["key"]): e for e in events if e["event"] == "layer_read_submitted"
    }
    read_ends = {
        tuple(e["key"]): e for e in events if e["event"] == "layer_read_complete"
    }
    computes = {
        (e["worker"], e["step_id"], e["layer"]): e
        for e in events
        if e["event"] == "layer_compute_started"
    }
    compute_ends = {
        (e["worker"], e["step_id"], e["layer"]): e
        for e in events
        if e["event"] == "layer_compute_complete"
    }
    assert read_starts.keys() == read_ends.keys()
    assert computes.keys() == compute_ends.keys()
    layers = r["config"]["sa_profile"]["L"]
    for key, step in steps.items():
        assert (
            0
            < step["num_requests"]
            == len(step["num_scheduled_tokens"])
            <= r["config"]["max_num_seqs"]
        )
        assert (
            sum(step["num_scheduled_tokens"].values())
            <= r["config"]["max_num_batched_tokens"]
        )
        assert (
            abs(
                layer_compute(step["shapes"], r["config"]["sa_profile"])
                - step["layer_compute_s"]
            )
            < 1e-12
        )
        for layer in range(layers):
            k = (*key, layer)
            start, end = computes[k], compute_ends[k]
            assert abs(end["t"] - start["end_s"]) < 1e-12
            assert abs(end["t"] - start["t"] - step["layer_compute_s"]) < 1e-12
            if layer:
                assert start["t"] + 1e-12 >= compute_ends[(*key, layer - 1)]["t"]
            if step["layer_read_bytes"]:
                assert start["t"] + 1e-12 >= read_ends[k]["t"] >= read_starts[k]["t"]
                expected_submit = (
                    step["t"] if layer == 0 else computes[(*key, layer - 1)]["t"]
                )
                assert abs(read_starts[k]["t"] - expected_submit) < 1e-12
                assert abs(read_starts[k]["bytes"] - step["layer_read_bytes"]) < 1e-5
        assert abs(completed[key]["t"] - compute_ends[(*key, layers - 1)]["t"]) < 1e-12
    expected_bytes = (
        sum(s["h_tokens"] for s in trace)
        * r["config"]["storage"]["layer_bytes_per_token"]
        * layers
    )
    assert abs(r["bytes_read"] - expected_bytes) < 0.01
    assert abs(r["submitted_read_bytes"] - expected_bytes) < 0.01
    assert (
        abs(
            sum(
                (e["t"] - e["from"]) * e["actual_Bps"]
                for e in events
                if e["event"] == "storage_interval"
            )
            - expected_bytes
        )
        < 0.1
    )
    assert all(
        e["actual_Bps"] <= r["config"]["storage"]["shared_link_Bps"] + 0.01
        for e in events
        if e["event"] == "storage_interval"
    )
    for name in (
        "request_arrived",
        "request_submitted",
        "admission_released",
        "first_token",
        "request_finished",
    ):
        assert Counter(e["id"] for e in events if e["event"] == name) == Counter(
            r["requests"].keys()
        ), name
    for row in trace:
        s = r["requests"][row["id"]]
        assert all(
            s[k] == row[k]
            for k in ("arrival_s", "h_tokens", "u_tokens", "class_id", "deadline_s")
        )
        assert s["arrival_s"] <= s["submitted_s"] <= s["first_token_s"] == s["finish_s"]
        assert s["external_tokens"] == row["h_tokens"]
        assert s["preemptions"] == 0 and s["load_attempts"] == int(row["h_tokens"] > 0)
        assert not s["lease_open"] and s["state"] == "FINISHED"
        check_profile(row, s, r["config"])
        if row["h_tokens"]:
            assert s["load_submit_s"] <= s["load_done_s"] <= s["first_token_s"]
    for e in events:
        if (
            e["event"] == "admission_reserved"
            and e["mode"] in ("tfs", "quota_fcfs")
            and e["heavy"]
        ):
            assert e["held_after"] <= e["cap"]
    print(
        f"{directory}: exact SA inputs / layer causality / bytes / memory / native lifecycle PASS"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("directories", nargs="+")
    for directory in p.parse_args().directories:
        check(directory)
