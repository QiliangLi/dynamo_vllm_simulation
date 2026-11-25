"""Statistics and decision rules of the search. FROZEN: the orchestrator pins the
sha256 of this file at start-up and refuses to run if it changes.

Everything compares a candidate with a reference (the baseline, or the current
champion) on two client-side metrics: mean TTFT and makespan. Improvements are
relative to the reference mean and positive is better.
"""
import math

Z95 = 1.645  # one-sided 95%


def _mean(values):
    return math.fsum(values) / len(values)


def _sd(values):
    if len(values) < 2:
        return 0.0
    center = _mean(values)
    return math.sqrt(math.fsum((v - center) ** 2 for v in values) / (len(values) - 1))


def stats(values):
    values = [float(v) for v in values]
    sd = _sd(values)
    return dict(n=len(values), mean=_mean(values) if values else 0.0, sd=sd,
                se=sd / math.sqrt(len(values)) if values else 0.0)


def improvement(reference, candidate):
    """(reference mean - candidate mean) / reference mean."""
    return (reference["mean"] - candidate["mean"]) / reference["mean"]


def improvement_lb(reference, candidate):
    """One-sided 95% lower bound of the relative improvement."""
    se = math.sqrt(reference["se"] ** 2 + candidate["se"] ** 2)
    return improvement(reference, candidate) - Z95 * se / reference["mean"]


def regression(reference, candidate):
    """True when the candidate is worse than the reference beyond noise on one metric."""
    se = math.sqrt(reference["se"] ** 2 + candidate["se"] ** 2)
    tolerance = max(0.005 * reference["mean"], Z95 * se)
    return candidate["mean"] - reference["mean"] > tolerance


def workload_result(reference, candidate):
    """reference, candidate: {workload: {'ttft': [..], 'makespan': [..]}}."""
    out = {}
    for workload in reference:
        ref, cand = reference[workload], candidate.get(workload)
        if not cand or not cand["ttft"] or not cand["makespan"]:
            continue
        t_ref, t_cand = stats(ref["ttft"]), stats(cand["ttft"])
        m_ref, m_cand = stats(ref["makespan"]), stats(cand["makespan"])
        ttft, makespan = improvement(t_ref, t_cand), improvement(m_ref, m_cand)
        out[workload] = dict(
            ttft=ttft, makespan=makespan,
            ttft_lb=improvement_lb(t_ref, t_cand), makespan_lb=improvement_lb(m_ref, m_cand),
            per=0.5 * (ttft + makespan),
            regresses=regression(t_ref, t_cand) or regression(m_ref, m_cand),
            cand_ttft=t_cand["mean"], cand_makespan=m_cand["mean"], cand_n=t_cand["n"],
            ref_ttft=t_ref["mean"], ref_makespan=m_ref["mean"])
    return out


def total_score(results):
    """0.5 * mean over workloads + 0.5 * worst workload. None when no workload has data."""
    if not results:
        return None
    per = [row["per"] for row in results.values()]
    return 0.5 * _mean(per) + 0.5 * min(per)


def total_score_lb(results):
    if not results:
        return None
    per = [0.5 * (row["ttft_lb"] + row["makespan_lb"]) for row in results.values()]
    return 0.5 * _mean(per) + 0.5 * min(per)


def screen_pass(results):
    """Both metrics improve on average across workloads, and no workload regresses badly."""
    if not results:
        return False, "没有可用结果"
    ttft = _mean([row["ttft"] for row in results.values()])
    makespan = _mean([row["makespan"] for row in results.values()])
    if ttft <= 0 or makespan <= 0:
        return False, "平均 TTFT 改进 %.1f%%，完成时间改进 %.1f%%，方向不全对" % (100 * ttft, 100 * makespan)
    worst = min(results.items(), key=lambda item: min(item[1]["ttft"], item[1]["makespan"]))
    if min(worst[1]["ttft"], worst[1]["makespan"]) < -0.10:
        return False, "%s 上有一项回退超过 10%%" % worst[0]
    return True, "通过"


def confirm_pass(results):
    if not results:
        return False, "没有可用结果"
    score = total_score(results)
    if score is None or score <= 0:
        return False, "排序分不高于参考（%.2f%%）" % (100 * score)
    worst = min(results.items(), key=lambda item: min(item[1]["ttft"], item[1]["makespan"]))
    if min(worst[1]["ttft"], worst[1]["makespan"]) < -0.05:
        return False, "%s 上有一项回退超过 5%%" % worst[0]
    return True, "通过"


def decide_promotion(results):
    """Section 10. Score above the reference with 95% confidence, and no workload regresses."""
    if not results:
        return dict(promote=False, score=None, score_lb=None, reasons=["没有可用结果"], regressions=[])
    score, score_lb = total_score(results), total_score_lb(results)
    regressions = sorted(w for w, row in results.items() if row["regresses"])
    reasons = []
    if score is None or score_lb <= 0:
        reasons.append("排序分下界不高于参考（%.2f%%）" % (100 * (score_lb or 0)))
    if regressions:
        reasons.append("这些负载上有超出噪声的回退：%s" % "、".join(regressions))
    return dict(promote=not reasons, score=score, score_lb=score_lb, reasons=reasons,
                regressions=regressions)


def needs_more_runs(results, reference, candidate, target):
    """Workloads whose score is still too uncertain to rank against the champion."""
    uncertain = []
    for workload, row in results.items():
        if row["cand_n"] >= target:
            continue
        se = 0.5 * (abs(row["ttft"] - row["ttft_lb"]) + abs(row["makespan"] - row["makespan_lb"])) / Z95
        if abs(row["per"]) < 1.5 * se:
            uncertain.append(workload)
    return uncertain


def control_ok(reference, value_ttft, value_makespan, z=3.0):
    """A light control run must stay inside mean +/- z sd of the baseline on both metrics."""
    out = []
    for name, value in (("ttft", value_ttft), ("makespan", value_makespan)):
        ref = stats(reference[name])
        if ref["sd"] > 0 and abs(value - ref["mean"]) > z * ref["sd"]:
            out.append(name)
    return out
