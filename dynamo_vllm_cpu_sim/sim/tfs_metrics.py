"""只从记录事实计算评价指标；不把统计结果反馈给控制器。"""

from collections import Counter, defaultdict
import math
import statistics


def distribution(values):
    xs = sorted(values)
    return {"n": len(xs), "mean": statistics.mean(xs) if xs else None,
            **{f"p{q}": xs[max(0, math.ceil(q / 100 * len(xs)) - 1)] if xs else None
               for q in (50, 95, 99)}, "small_sample": len(xs) < 100}


def summarize(stats, events, span, fixed_slo):
    """按真实请求时间与 SchedulerOutput 统计；空 Connector step 不计入 batch。"""
    groups = defaultdict(list)
    for s in stats.values():
        groups[s["class_id"]].append(s)
    classes = {}
    for cls, rows in sorted(groups.items()):
        classes[cls] = {key: distribution([r[key] for r in rows if r[key] is not None])
                        for key in ("ttft_s", "central_wait_s", "engine_ttft_s",
                                    "load_submit_wait_s", "load_duration_s")}
        fixed = sum(r["ttft_s"] <= fixed_slo for r in rows)
        attained = sum(r["first_token_s"] <= r["deadline_s"] for r in rows)
        classes[cls].update(requests=len(rows), ttft_slo_attainment=fixed / len(rows),
                            profile_deadline_attainment=attained / len(rows),
                            fixed_goodput_req_s=fixed / span,
                            profile_goodput_req_s=attained / span,
                            estimated_read_bytes=sum(r["estimated_read_bytes"] for r in rows),
                            submitted_read_bytes=sum(r["submitted_read_bytes"] for r in rows))
    steps = [e for e in events if e["event"] == "step_scheduled"]
    counts = Counter(e["num_requests"] for e in steps)
    weighted = defaultdict(float)
    for e in steps:
        weighted[str(e["num_requests"])] += e["duration_s"]
    time = sum(weighted.values())
    return {"classes": classes,
            "actual_batch": {"requests": distribution([e["num_requests"] for e in steps]),
                             "request_count_histogram": dict(counts),
                             "time_weighted_histogram_s": dict(weighted),
                             "time_weighted_mean": sum(int(k)*v for k,v in weighted.items()) / time if time else 0,
                             "prefill_tokens": distribution([e["prefill_tokens"] for e in steps]),
                             "decode_tokens": distribution([e["decode_tokens"] for e in steps])}}
