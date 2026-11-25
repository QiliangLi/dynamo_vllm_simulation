"""Per-run diagnostics for the tuning workflow: where the time went, in numbers.

Reads one finished run directory and its benchmark directory. Pure standard
library; runs inside WSL with the same interpreter as the runner.
"""
import csv
import json
import statistics
from pathlib import Path

BIN_S = 0.25


def _read(path):
    return json.loads(Path(path).read_bytes())


def short(rid):
    """The client names a request by the first 32 hex digits of its dashed id."""
    return rid.replace("-", "")[:32]


def no_contention_reference(items):
    """Every request at its standalone speed, same worker split, FCFS per worker."""
    queues = {}
    for item in items:
        queues.setdefault(item["worker"], []).append(item)
    ttfts, finishes = [], []
    for queue in queues.values():
        queue.sort(key=lambda item: item["arrival"])
        clock = 0.0
        for item in queue:
            start = max(clock, item["arrival"])
            ttfts.append(start + item["service"] - item["arrival"])
            finishes.append(start + item["service"])
            clock = start + item["service"]
    first = min(item["arrival"] for item in items)
    return dict(mean_ttft_s=statistics.mean(ttfts), makespan_s=max(finishes) - first)


def demand_profile(intervals, link_Bps):
    """In-flight demand over time. ``intervals``: [(start, end, rate_Bps)].

    Overload seconds come from a sweep line over the union of the intervals.
    """
    events = []
    for start, end, rate in intervals:
        if rate > 0 and end > start:
            events.append((start, rate))
            events.append((end, -rate))
    events.sort()
    overload = near = integral = 0.0
    current = peak = 0.0
    previous = None
    for t, delta in events:
        if previous is not None and current > 0:
            width = t - previous
            integral += current * width
            if current > link_Bps * (1 + 1e-9):
                overload += width
            elif current > 0.8 * link_Bps:
                near += width
        current += delta
        peak = max(peak, current)
        previous = t
    return dict(overload_s=overload, near_overload_s=near,
                peak_demand_GBs=peak / 1e9, demand_GB=integral / 1e9)


def merge_spans(intervals):
    """Union of [(start, end, tag)], keeping the tag of the first interval."""
    merged = []
    for start, end, tag in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, tag])
    return merged


def digest(run, bench_dir, summary):
    """Everything the analyst and the gates need about one run, as plain numbers."""
    run, bench_dir = Path(run), Path(bench_dir)
    client, final = _read(run / "report.json"), _read(run / "admission.final.json")
    records, storage = final["records"], _read(run / "storage.final.json")
    check, gate, layer_check = (_read(run / name) for name in
                                ("check.json", "read_admission.check.json", "layer_check.json"))
    with (bench_dir / "requests.csv").open(encoding="utf-8-sig") as stream:
        rows = {row["id"]: row for row in csv.DictReader(stream)}
    source_of = {value: key for key, value in _read(run / "request_ids.json").items()}
    hints = final.get("storage_hints") or {}
    requests = client["requests"]
    first_send = min(r["sent_perf_s"] for r in requests.values())

    # The admission service and the store keep their own model clocks. Line them
    # up with the client's clock through the HTTP first token of every request.
    offsets = [records[short(rid)]["first_token_s"] - (r["sent_perf_s"] - first_send + r["ttft_s"])
               for rid, r in requests.items()
               if short(rid) in records and records[short(rid)].get("first_token_s") is not None]
    axis = statistics.median(offsets) if offsets else 0.0
    origin = final.get("origin_monotonic_s")
    store_shift = (origin - storage["t0_monotonic_s"]) if origin is not None else 0.0

    items, rate_of = [], {}
    for rid, r in requests.items():
        profile = records[short(rid)]["profile"] if short(rid) in records else {}
        rate = profile.get("pressure_Bps", 0.0)
        rate_of[rid] = rate
        src = source_of[rid]
        hint = hints.get(rid) or hints.get(short(rid)) or {}
        items.append(dict(rid=rid, cls=rows[src]["class_id"] if src in rows else "?",
                          ttft=r["ttft_s"], arrival=r["planned_arrival_s"],
                          actual_arrival=r["sent_perf_s"] - first_send,
                          worker=summary["worker_of"][rid], pressure_GBs=rate / 1e9,
                          read_bytes=int(profile.get("estimated_read_bytes", 0)),
                          service=profile.get("reference_service_s", r["e2e_s"]),
                          heavy=bool(hint.get("is_heavy_read"))))
    arrival_of = {item["rid"]: item["actual_arrival"] for item in items}

    link = storage["shared_link_Bps"]
    bins = int(summary["makespan_s"] / BIN_S) + 1
    demand_bins, active_bins = [0.0] * bins, [0.0] * bins

    def add(series, start, end, weight):
        for i in range(max(0, int(start / BIN_S)), min(bins, int(end / BIN_S) + 1)):
            lo, hi = max(start, i * BIN_S), min(end, (i + 1) * BIN_S)
            if hi > lo:
                series[i] += weight * (hi - lo) / BIN_S

    span = {}
    for row in storage["layer_read_history"]:
        rate = rate_of.get(short(row["rid"]), 0.0)
        if rate <= 0:
            continue
        start = row["submitted_s"] + store_shift - axis
        end = row["completed_s"] + store_shift - axis
        add(demand_bins, start, end, rate / 1e9)
        first, last = span.get(row["rid"], (start, end))
        span[row["rid"]] = (min(first, start), max(last, end))
    read_intervals = [(start, end, rate_of[short(rid)]) for rid, (start, end) in span.items()]

    # Steps: a worker runs one request at a time, so a request occupies its worker
    # from its first layer's compute start to its last layer's compute complete.
    per_worker, read_wait = {}, 0.0
    for index, path in enumerate(sorted(run.glob("worker*.steps.jsonl"))):
        step_of, spans = {}, {}
        if origin is None:
            per_worker[index] = []
            continue
        with path.open() as stream:
            for event in map(json.loads, stream):
                kind = event.get("event")
                if kind == "layer_compute_start":
                    rid = event["req_ids"][0]
                    step_of[event["step"]] = rid
                    start, end = spans.get(rid, (event["occurred_mono"], event["occurred_mono"]))
                    spans[rid] = (min(start, event["occurred_mono"]), end)
                elif kind == "layer_compute_complete":
                    rid = step_of.get(event["step"])
                    if rid in spans:
                        spans[rid] = (spans[rid][0], max(spans[rid][1], event["occurred_mono"]))
                elif "num_scheduled_tokens" in event:
                    read_wait += max(0.0, event.get("sleep_elapsed_model_s", 0.0) - event["timing"]["duration_s"])
        per_worker[index] = [(start - origin - axis, end - origin - axis, rid) for rid, (start, end) in spans.items()]

    busy_total, starved, longest, all_busy, per_worker_load = 0.0, 0.0, 0.0, [], []
    for index in sorted(per_worker):
        merged = merge_spans(per_worker[index])
        busy = sum(end - start for start, end, _ in merged)
        per_worker_load.append(dict(worker=index, requests=len(per_worker[index]), busy_s=busy))
        busy_total += busy
        all_busy.extend(merged)
        for start, end in ((s, e) for s, e, _ in merged):
            add(active_bins, start, end, 1.0)
        for i in range(len(merged) - 1):
            gap = merged[i + 1][0] - merged[i][1]
            longest = max(longest, gap)
            # Starved: idle while the next request of this worker had already arrived.
            if arrival_of.get(short(merged[i + 1][2]), 1e18) <= merged[i][1] + 0.05:
                starved += gap
    window = (min(b[0] for b in all_busy), max(b[1] for b in all_busy)) if all_busy else (0.0, 0.0)
    workers = summary["workers"]
    idle = max(0.0, workers * (window[1] - window[0]) - busy_total)

    classes = {}
    for item in items:
        bucket = classes.setdefault(item["cls"], dict(ttft=[], pressure=[], read_bytes=0, heavy=0, arrival=[]))
        bucket["ttft"].append(item["ttft"])
        bucket["pressure"].append(item["pressure_GBs"])
        bucket["read_bytes"] += item["read_bytes"]
        bucket["heavy"] += item["heavy"]
        bucket["arrival"].append(item["actual_arrival"])
    class_stats = {}
    for name, bucket in sorted(classes.items()):
        ordered = sorted(bucket["ttft"])
        class_stats[name] = dict(n=len(ordered), heavy_share=bucket["heavy"] / len(ordered),
                                 mean_arrival_s=statistics.mean(bucket["arrival"]), mean_ttft_s=statistics.mean(ordered),
                                 p95_ttft_s=ordered[int(.95 * (len(ordered) - 1) + 1e-9)],
                                 max_ttft_s=ordered[-1],
                                 mean_pressure_GBs=statistics.mean(bucket["pressure"]),
                                 read_GB=bucket["read_bytes"] / 1e9)

    denied, granted = {}, {}
    theta_final = cap_final = None
    for event in final["events"]:
        if event["event"] == "read_permit_denied":
            denied.setdefault(event["id"], event["t"])
        elif event["event"] == "admission_reserved":
            granted.setdefault(event["id"], event["t"])
        elif event["event"] == "threshold_updated":
            theta_final, cap_final = event.get("theta"), event.get("cap")
    waits = [granted[rid] - start for rid, start in denied.items() if rid in granted]

    positive = sorted(item["pressure_GBs"] for item in items if item["pressure_GBs"] > 0)
    profile = demand_profile(read_intervals, link)
    layer_profile = demand_profile(
        [(row["submitted_s"], row["completed_s"], rate_of.get(short(row["rid"]), 0.0))
         for row in storage["layer_read_history"]], link)
    fine = 0.01
    delivered = {}
    for row in storage["layer_read_history"]:
        width = max(row["completed_s"] - row["submitted_s"], 1e-9)
        for i in range(int(row["submitted_s"] / fine), int(row["completed_s"] / fine) + 1):
            lo, hi = max(row["submitted_s"], i * fine), min(row["completed_s"], (i + 1) * fine)
            if hi > lo:
                delivered[i] = delivered.get(i, 0.0) + row["bytes"] * (hi - lo) / width
    saturated = sum(fine for value in delivered.values() if value / fine >= 0.9 * link)
    stretches = sorted((row["completed_s"] - row["submitted_s"]) / (row["bytes"] / link)
                       for row in storage["layer_read_history"] if row["bytes"])
    profile.update(
        layer_overload_s=layer_profile["overload_s"], saturated_s=saturated,
        read_stretch_median=stretches[len(stretches) // 2] if stretches else 0.0,
        read_stretch_p95=stretches[int(.95 * (len(stretches) - 1) + 1e-9)] if stretches else 0.0)
    link_time = check["actual_submitted_read_bytes"] / link
    reference = no_contention_reference(items)
    return dict(
        overload=dict(profile, link_time_s=link_time, transfer_span_s=check["transfer_span_s"],
                      link_utilization=link_time / check["transfer_span_s"] if check["transfer_span_s"] else 0.0),
        workers=dict(count=workers, busy_s=busy_total, window_s=window[1] - window[0], idle_s=idle,
                     idle_mean_s=idle / workers, starved_idle_s=starved, longest_idle_s=longest,
                     per_worker=per_worker_load),
        read_wait_s=read_wait, compute_model_s=layer_check.get("compute_model_s"),
        model_sum_s=check["model_sum_s"],
        reference=dict(mean_ttft_s=reference["mean_ttft_s"], makespan_s=reference["makespan_s"],
                       compute_floor_s=check["model_sum_s"] / workers, link_floor_s=link_time),
        permits=dict(heavy_hints=gate["heavy_hints"], grants=gate["grants"], denied=gate["denied"],
                     theta_final_Bps=theta_final, cap_final=cap_final,
                     peak_held=check["held_heavy_peak"], blocked_requests=len(denied),
                     blocked_total_s=sum(waits), blocked_max_s=max(waits, default=0.0)),
        waits=dict(admission_mean_s=check["central_wait_mean_s"], admission_max_s=check["central_wait_max_s"]),
        pressure_GBs=dict(zero_read_share=1 - len(positive) / len(items),
                          min=positive[0] if positive else 0.0,
                          median=statistics.median(positive) if positive else 0.0,
                          p75=positive[int(.75 * (len(positive) - 1) + 1e-9)] if positive else 0.0,
                          max=positive[-1] if positive else 0.0,
                          over_half_link=sum(p > link / 2e9 for p in positive) / len(positive) if positive else 0.0),
        classes=class_stats,
        timeline=dict(bin_s=BIN_S, demand_GBs=[round(v, 3) for v in demand_bins],
                      active_requests=[round(v, 2) for v in active_bins]))
