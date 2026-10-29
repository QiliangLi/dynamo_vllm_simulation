"""只读复核 SA 正式归档、准入轨迹与指标；不重新运行正式矩阵。"""

import csv, hashlib, json, math, statistics, subprocess, sys, contextlib, re
from collections import defaultdict, Counter
from fractions import Fraction as F
from pathlib import Path

R = Path(__file__).resolve().parents[2]
P = R / "dynamo_vllm_cpu_sim"
A = P / "results/sa_alignment_summary_20261019"
RAW = P / "results/sa_alignment_20261019"
O = P / "results/sa_code_review_20261019"
O.mkdir(exist_ok=True)


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def j(p):
    return json.loads(p.read_text())


def csvrows(p):
    return list(csv.DictReader(p.open()))


def git(*a):
    return subprocess.check_output(["git", *a], cwd=R, text=True).strip()


def same(a, b, tol=1e-10):
    assert math.isclose(a, b, rel_tol=tol, abs_tol=tol), (a, b)


source = j(A / "source/source.json")
manifest = j(A / "manifest.json")
provenance = j(A / "runtime-provenance.json")
result = {
    "scope": {
        "base": git("rev-parse", "a870596"),
        "tfs": git("rev-parse", "344f623"),
        "sa": git("rev-parse", "e6a0825"),
        "reviewed_head": git("rev-parse", "8ec666f"),
    },
    "checks": {},
}
impl = hashlib.sha256(
    b"".join(p.name.encode() + p.read_bytes() for p in sorted((P / "sim").glob("*.py")))
).hexdigest()
assert impl == provenance["matrix_implementation_sha256"]
result["implementation_sha256"] = impl
protected = []
for f, v in provenance["protected_sources"].items():
    actual = sha(P / f)
    assert actual == v["expected"] == v["actual"]
    protected.append({"file": f, "sha256": actual})
result["protected_sources"] = protected
unmodified = [
    "sim/router.py",
    "sim/connector.py",
    "sim/storage.py",
    "versions.json",
    "source-fingerprints.json",
    "patched/ascend_balance.patch",
]
for f in unmodified:
    assert (
        subprocess.check_output(
            ["git", "show", "a870596:dynamo_vllm_cpu_sim/" + f], cwd=R
        )
        == (P / f).read_bytes()
    ), f
result["unchanged_from_base"] = unmodified
sa = R.parent / "status_aware_network"
assert (
    subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=sa, text=True).strip()
    == source["commit"]
)
for f, h in source["source_sha256"].items():
    assert sha(sa / f) == h
result["sa_source"] = {
    "commit": source["commit"],
    "protected_files": len(source["source_sha256"]),
    "worktree_clean": not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=sa, text=True
    ).strip(),
}
hashes = csvrows(A / "raw_hashes.csv")
for h in hashes:
    assert sha(RAW / h["cell"] / h["file"]) == h["sha256"], h
result["checks"]["raw_hashes"] = {"matched": len(hashes)}
request_rows = csvrows(A / "requests.csv")
rqmap = {}
for row in request_rows:
    rqmap[
        (
            row["variant"],
            int(row["seed"]),
            int(row["cap"]),
            row["mode"],
            int(row["repeat"]),
            row["id"],
        )
    ] = row
assert len(rqmap) == len(request_rows) == 34560
runrows = csvrows(A / "runs.csv")
runmap = {
    (x["variant"], int(x["seed"]), int(x["cap"]), x["mode"], int(x["repeat"])): x
    for x in runrows
}
summary = []
event_count = reserve_count = threshold_count = step_count = chunk_count = (
    mixed_count
) = 0
min_margin = math.inf
max_layer_err = 0
max_ttft_err = 0
sys.path.insert(0, str(P))
from scripts.check_sa_results import check

with (O / "rechecked-runs.log").open("w") as log, contextlib.redirect_stdout(log):
    for job in manifest:
        folder = Path(job["output"])
        check(folder)
        report = j(folder / "report.json")
        cfg = report["config"]
        identity = tuple(job[k] for k in ("variant", "seed", "cap", "mode", "repeat"))
        assert report["implementation_sha256"] == impl
        assert cfg == job["config"] and cfg["source_sha256"] == source["source_sha256"]
        assert cfg["workers"] == 32 and cfg["storage"] == {
            "bandwidth_policy": "sa_max_min",
            "shared_link_Bps": 120e9,
            "q_max_Bps": 200e9,
            "layer_bytes_per_token": 4096,
            "read_latency_s": 0,
        }
        assert cfg["admission"] == {
            **cfg["admission"],
            "mode": job["mode"],
            "quantile": 0.75,
            "reference_bandwidth_Bps": 120e9,
            "release_on": "first_token",
            "aging_enabled": False,
            "feedback_delay_s": 0,
        }
        trace = j(Path(job["trace"]))
        refs = {}
        byid = {s["id"]: s for s in trace}
        prof = cfg["sa_profile"]
        for row in trace:
            h, u = row["h_tokens"], row["u_tokens"]
            eta = max(F(prof["eta_floor"]), min(F(1), F(u, prof["N_sat"])))
            c = (
                F(prof["t_launch_s"])
                + F(prof["a_s_per_token"]) * u / eta
                + F(prof["b_s_per_pair"]) * (u * h + u * (u + 1) // 2)
            )
            v = F(prof["kappa_gb_per_token_layer"]) * 10**9 * h
            read = v / F(120 * 10**9)
            t0 = read + prof["L"] * c + (prof["L"] - 1) * max(F(0), read - c)
            refs[row["id"]] = {"pressure": float(v / c), "t0": float(t0)}
            same(row["T0_s"], float(t0))
            same(row["deadline_s"], row["arrival_s"] + 4 * float(t0))
        attained = 0
        times = []
        for rid, s in report["requests"].items():
            row = byid[rid]
            exported = rqmap[(*identity, rid)]
            for field, value in exported.items():
                if field in ("variant", "seed", "cap", "mode", "repeat", "id"):
                    continue
                actual = s[field]
                if actual is None:
                    assert value == ""
                elif isinstance(actual, (int, float)):
                    same(float(value), actual, 1e-12)
                else:
                    assert value == str(actual)
            same(s["pressure_Bps"], refs[rid]["pressure"], 1e-12)
            same(s["reference_service_s"], refs[rid]["t0"])
            ttft = s["first_token_s"] - row["arrival_s"]
            max_ttft_err = max(max_ttft_err, abs(ttft - s["ttft_s"]))
            times.append(ttft)
            attained += s["first_token_s"] <= row["deadline_s"]
        value = {
            "variant": job["variant"],
            "seed": job["seed"],
            "cap": job["cap"],
            "mode": job["mode"],
            "repeat": job["repeat"],
            "slo": attained / 360,
            "ttft_ms": statistics.mean(times) * 1000,
        }
        same(value["slo"], report["profile_deadline_attainment"], 1e-12)
        same(value["ttft_ms"], report["mean_ttft_s"] * 1000, 1e-12)
        for field in ("slo", "ttft_ms"):
            same(value[field], float(runmap[identity][field]), 1e-12)
        summary.append(value)
        events = [
            json.loads(line)
            for line in (folder / "events.jsonl").read_text().splitlines()
        ]
        event_count += len(events)
        arrived = set()
        pending = set()
        leases = set()
        theta = None
        cap = None
        prefill = Counter()
        for ev in events:
            kind = ev["event"]
            now = ev["t"]
            if kind == "request_arrived":
                rid = ev["id"]
                assert rid not in arrived and byid[rid]["arrival_s"] <= now
                arrived.add(rid)
                pending.add(rid)
            elif kind == "threshold_updated":
                xs = sorted(refs[r]["pressure"] for r in arrived)
                raw = xs[math.floor(0.75 * (len(xs) - 1))]
                theta = raw
                cap = max(1, math.floor(120e9 / theta)) if theta else 1 << 30
                same(ev["theta"], theta, 1e-12)
                assert ev["cap"] == cap and ev["history_size"] == len(arrived)
                threshold_count += 1
            elif kind == "admission_reserved":
                limited = job["mode"] in ("quota_fcfs", "tfs")
                heavy = lambda rid: refs[rid]["pressure"] >= theta
                held = sum(heavy(rid) for rid in leases)
                feasible = [
                    rid
                    for rid in pending
                    if not limited or not heavy(rid) or held < cap
                ]

                def key(rid):
                    r = byid[rid]
                    return (
                        (r["arrival_s"], rid)
                        if job["mode"] in ("immediate", "quota_fcfs")
                        else (
                            r["deadline_s"] - now - refs[rid]["t0"],
                            r["deadline_s"],
                            rid,
                        )
                    )

                assert ev["id"] == min(feasible, key=key), (
                    job,
                    ev["id"],
                    min(feasible, key=key),
                )
                assert ev["held_before"] == held and ev["reason"] == "normal"
                pending.remove(ev["id"])
                leases.add(ev["id"])
                reserve_count += 1
            elif kind == "admission_released":
                assert ev["id"] in leases
                leases.remove(ev["id"])
                assert ev["reason"] == "first_token"
            elif kind == "step_started":
                step_count += 1
                shapes = []
                for rid, count in ev["num_scheduled_tokens"].items():
                    shapes.append((byid[rid]["h_tokens"] + prefill[rid], count))
                    prefill[rid] += count
                    if prefill[rid] < byid[rid]["u_tokens"]:
                        chunk_count += 1
                assert shapes == [tuple(s) for s in ev["shapes"]]
                n = sum(u for h, u in shapes)
                pairs = sum(u * h + u * (u + 1) // 2 for h, u in shapes)
                eta = max(F(prof["eta_floor"]), min(F(1), F(n, prof["N_sat"])))
                expected = float(
                    F(prof["t_launch_s"])
                    + F(prof["a_s_per_token"]) * n / eta
                    + F(prof["b_s_per_pair"]) * pairs
                )
                max_layer_err = max(
                    max_layer_err, abs(expected - ev["layer_compute_s"])
                )
                same(expected, ev["layer_compute_s"], 1e-12)
                mixed_count += (
                    len({byid[rid]["class_id"] for rid in ev["num_scheduled_tokens"]})
                    > 1
                )
        assert not pending and not leases and len(arrived) == 360
        assert all(prefill[rid] == r["u_tokens"] for rid, r in byid.items())
        min_margin = min(min_margin, *[w["free_blocks"] for w in report["workers"]])
assert len(summary) == 96
perseed = defaultdict(list)
for r in summary:
    perseed[tuple(r[k] for k in ("variant", "seed", "cap", "mode"))].append(r)
groups = defaultdict(list)
for (v, s, c, m), rs in perseed.items():
    assert len(rs) == 3
    groups[(v, c, m)].append(
        {k: statistics.median(r[k] for r in rs) for k in ("slo", "ttft_ms")}
    )
for r in csvrows(A / "summary.csv"):
    rs = groups[(r["variant"], int(r["cap"]), r["mode"])]
    for k in ("slo", "ttft_ms"):
        same(statistics.median(s[k] for s in rs), float(r[k]), 1e-12)
result["checks"]["recomputed_metrics"] = {
    "runs": 96,
    "request_rows": 34560,
    "summary_rows": len(groups),
    "aggregation": "median of routing repeats per seed, then median across seeds",
    "max_ttft_error_s": max_ttft_err,
}
result["checks"]["independent_event_replay"] = {
    "events": event_count,
    "admission_decisions": reserve_count,
    "threshold_updates": threshold_count,
    "actual_steps": step_count,
    "chunked_request_steps": chunk_count,
    "mixed_steps": mixed_count,
    "max_layer_compute_error_s": max_layer_err,
    "all_passed": True,
}
result["checks"]["existing_checker_rerun"] = {
    "runs": 96,
    "passed": True,
    "log": "rechecked-runs.log",
}
result["historical_evidence"] = {
    "parity_cases": len(j(A / "verification.json")),
    "parity_max_error_s": max(x["max_abs_error_s"] for x in j(A / "verification.json")),
    "audit": j(A / "audit.json"),
}
result["limits"] = [
    "This review rechecks archived 96 runs; it does not rerun the full matrix.",
    "Independent numerical replay covers recorded decisions and profile formula, not numerical device execution.",
    "SA-vs-DV dispatch and admission lease populations differ; cap=1 does not make dispatch semantics identical.",
    "Multi-request batch execution has no independent SA end-to-end parity case in the eight archived cases.",
    "Source fingerprints cover named files, not every installed binary or all transitive dependencies.",
]
testlog = (O / "unit-tests.log").read_text()
match = re.search(r"Ran (\d+) tests in ([\d.]+)s", testlog)
assert match and testlog.rstrip().endswith("OK")
result["checks"]["fresh_unit_tests"] = {
    "tests": int(match[1]),
    "wall_s": float(match[2]),
    "passed": True,
    "log": "unit-tests.log",
    "sha256": sha(O / "unit-tests.log"),
}
imports = j(O / "runtime-imports.json")
for name, key in (
    ("Scheduler", "upstream/vllm/vllm/v1/core/sched/scheduler.py"),
    ("KVCacheManager", "upstream/vllm/vllm/v1/core/kv_cache_manager.py"),
):
    assert Path(imports[name]["path"]).resolve() == (P / key).resolve()
    assert imports[name]["sha256"] == sha(P / key)
assert (
    sha(Path(imports["DynamoExtension"]["path"]))
    == imports["DynamoExtension"]["sha256"]
)
result["runtime_imports"] = imports
(O / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result["checks"], ensure_ascii=False, indent=2))
