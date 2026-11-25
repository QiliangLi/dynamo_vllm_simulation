#!/usr/bin/env python
"""Build one four-worker harness, apply an optional candidate overlay, replay, summarize.

Runs inside WSL with the package venv. It mirrors ``scripts/run_4w25.sh`` step by
step, then adds what the tuning workflow needs: an overlay, a policy merge, an
optional port offset for parallel runs, a diff of the harness against the frozen
base, and a per-run digest (digest.py). Every run gets a fresh output directory.

Usage:
  run_tuning.py SPEC_JSON                   run one spec
  run_tuning.py --base-manifest OUT.json    hash a harness built with no overlay

Spec fields:
  out (new directory)   bench or input_dir   arm: light | tfs | candidate
  overlay (directory)   policy (object)      max_tier (default 0)
  port_offset           workers (default 4)  bench_dir (with a prebuilt input)
  export_dir            timeout_s (default 1500)
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DV = REPO / "dynamo_vllm_cpu_sim"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts" / "tuning"))
from digest import digest  # noqa: E402
from tuning import scope  # noqa: E402

PORT_LITERALS = ("28765", "28766", "28767", "29111")
PORT_RE = re.compile(r"\b(" + "|".join(PORT_LITERALS) + r")\b")


class RunError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_bytes())


def write_json(path, value):
    """Write ``value`` so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def sha256_file(path):
    digest_ = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest_.update(block)
    return digest_.hexdigest()


def tree_hashes(root):
    """sha256 of every regular file below ``root``. Symlinks are not followed."""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            out[str(path.relative_to(root)).replace(os.sep, "/")] = sha256_file(path)
    return out


def overlay_files(overlay):
    """Candidate overlay files as ``{package-relative path: absolute path}``."""
    out = {}
    if not overlay:
        return out
    overlay = Path(overlay)
    for base, dirs, files in os.walk(overlay):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            path = Path(base) / name
            out[str(path.relative_to(overlay)).replace(os.sep, "/")] = path
    return out


# ---------------------------------------------------------------- input and harness

def prepare_input(spec, out, python):
    """Same as the entry: export the frozen benchmark, or copy a prebuilt stress input."""
    input_dir = out / "input"
    if spec.get("bench"):
        with (out / "export.log").open("w") as log:
            subprocess.run([str(python), str(REPO / "benchmark" / "manage.py"), "export", spec["bench"],
                            "--output", str(input_dir)], check=True, stdout=log, stderr=subprocess.STDOUT)
    else:
        shutil.copytree(spec["input_dir"], input_dir, symlinks=True)
    return input_dir


def merge_policy(config, policy, max_tier):
    if not policy:
        return
    bad = scope.check_policy(policy, max_tier)
    if bad:
        raise RunError("policy keys outside the open scope: %s" % bad)
    for section, values in policy.items():
        if not isinstance(values, dict):
            raise RunError("policy.%s must be an object" % section)
        target = config.setdefault(section, {})
        for key, value in values.items():
            target[key] = value


def build_harness(spec, out, python):
    """The entry's harness sequence, then the overlay, the base diff and the port offset."""
    harness = out / "harness"
    harness.mkdir()
    for name in ("scripts", "sim", "patched"):
        shutil.copytree(DV / name, harness / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.symlink((DV / "upstream").resolve(), harness / "upstream")
    for item in ("configs", "minimax_prefill", "source-fingerprints.json"):
        os.symlink(DV / item, harness / item)
    subprocess.run([str(python), str(harness / "scripts" / "apply_ascend_patch.py")], check=True,
                   stdout=subprocess.DEVNULL)
    patches = REPO / "scripts" / "four_worker"
    for name in ["stack.patch"] + (["light.patch"] if spec.get("arm") == "light" else []):
        with (patches / name).open("rb") as stream:
            subprocess.run(["patch", "-s", "-d", str(harness), "-p1"], stdin=stream, check=True)

    files = overlay_files(spec.get("overlay"))
    bad = scope.check_overlay_paths(files, int(spec.get("max_tier") or 0))
    if bad:
        raise RunError("overlay files outside the open scope: %s" % bad)
    # The arm's own patch (light.patch) may change files; record exactly which.
    base = read_json(spec["base_manifest"])["harness"] if spec.get("base_manifest") else {}
    patched = tree_hashes(harness)
    arm_changed = sorted(k for k in patched if k in base and base[k] != patched[k])
    stray = [k for k in patched if k not in base] + [k for k in base if k not in patched]
    if base and stray:
        raise RunError("patched harness differs from the frozen base: %s" % stray)
    for rel, src in files.items():
        dest = harness / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    hashes = tree_hashes(harness)
    changed = sorted(k for k in hashes if k in base and base[k] != hashes[k])
    added = sorted(k for k in hashes if k not in base)
    removed = sorted(k for k in base if k not in hashes)
    expected = set(files) | set(arm_changed)
    stray = [k for k in changed + added if k not in expected]
    if base and (stray or removed):
        raise RunError("harness differs from the frozen base outside the overlay: %s removed=%s"
                       % (stray, removed))
    write_json(out / "harness_diff.json", dict(
        overlay_files=sorted(files), arm_files=arm_changed, changed=changed, added=added, removed=removed,
        sha256={k: hashes[k] for k in sorted(files)}))

    offset = int(spec.get("port_offset") or 0)
    if offset:
        for rel in spec.get("port_files") or []:
            path = harness / rel
            path.write_text(PORT_RE.sub(lambda m: str(int(m.group()) + offset), path.read_text()))
    return harness


def kill_run_processes(harness, out):
    """Terminate anything still running inside this run's harness or output directory."""
    targets = (str(harness), str(out / "run"))
    victims = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            cwd = os.path.realpath("/proc/%s/cwd" % entry)
            cmd = open("/proc/%s/cmdline" % entry, "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(cwd == t or t in cmd for t in targets):
            victims.append(int(entry))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(3)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def replay(spec, harness, out, python, input_dir):
    server = Path(spec.get("server") or Path.home() / ".cache/dynamo-server-sim/build-1.2/python")
    admission = (spec.get("policy") or {}).get("admission") or {}
    env = dict(os.environ, PYTHONHASHSEED="0", HF_HUB_OFFLINE="1", VLLM_PLUGINS="", TMPDIR="/tmp",
               DYN_TCP_RPC_HOST="127.0.0.1", DYN_TCP_REQUEST_TIMEOUT="60", DYN_TFS_CONTROL_TIMEOUT_MS="3000")
    cmd = [str(python), "scripts/run_tfs_stack.py", "--config", str(input_dir / "config.json"),
           "--trace", str(input_dir / "trace.jsonl"), "--workers", str(int(spec.get("workers") or 4)),
           "--mode", "tfs"]
    # The entry forces --no-idle-bypass. A candidate may opt back in through its policy.
    if admission.get("idle_bypass") is not True:
        cmd.append("--no-idle-bypass")
    cmd += ["--server-dynamo", str(server), "--output", str(out / "run")]
    with (out / "run.log").open("w") as log:
        proc = subprocess.Popen(cmd, cwd=harness, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=float(spec.get("timeout_s") or 1500))
        except subprocess.TimeoutExpired:
            kill_run_processes(harness, out)
            raise RunError("replay timed out")
    if code != 0:
        raise RunError("replay exited with %d" % code)


# ---------------------------------------------------------------- summary

def summarize(run, bench_dir, arm):
    """Client-side metrics: the formulas of scripts/four_worker/summary.py, any worker count."""
    def need(condition, message):
        if not condition:
            raise RunError(message)

    manifest = read_json(bench_dir / "manifest.json")
    client, final = read_json(run / "report.json"), read_json(run / "admission.final.json")
    requests, hints = client["requests"], final["storage_hints"]
    mapping = read_json(run / "request_ids.json")
    with (bench_dir / "requests.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    check, gate, layers = (read_json(run / name) for name in
                           ("check.json", "read_admission.check.json", "layer_check.json"))
    need(check["status"] == gate["status"] == layers["status"] == "pass", "a run check did not pass")
    need(len(requests) == client["n_ok"] == client["n_total"] == manifest["requests"],
         "request count differs from the manifest")
    need(set(requests) == {mapping[row["id"]] for row in rows}, "requests differ from the benchmark input")
    need(check["actual_submitted_read_bytes"] == manifest["read_bytes"], "read bytes differ from the manifest")
    need(all(hints[mapping[row["id"]]]["estimated_remote_read_bytes"] == int(row["read_bytes"]) for row in rows),
         "hint bytes differ from requests.csv")
    marked = {row["id"] for row in rows if hints[mapping[row["id"]]]["is_heavy_read"]}
    if arm == "light":
        need(not marked and gate["grants"] == 0, "the light arm marked or granted something")

    workers = sorted(run.glob("worker*.steps.jsonl"))
    first = min(r["sent_perf_s"] for r in requests.values())
    short = {rid: rid.replace("-", "")[:32] for rid in requests}
    finishes, seen, worker_of = [], set(), {}
    for index, path in enumerate(workers):
        ids = set()
        with path.open() as stream:
            for event in map(json.loads, stream):
                if event.get("event") == "layer_compute_start":
                    ids.update(rid.replace("-", "")[:32] for rid in event["req_ids"])
        need(ids and ids.isdisjoint(seen), "a request ran on more than one worker")
        seen.update(ids)
        own = [rid for rid in requests if short[rid] in ids]
        finishes.append(max(requests[rid]["sent_perf_s"] - first + requests[rid]["e2e_s"] for rid in own))
        for rid in own:
            worker_of[rid] = index
    need(seen == set(short.values()), "some requests never ran")
    ttfts = sorted(r["ttft_s"] for r in requests.values())
    return dict(requests=len(requests), workers=len(workers), heavy=len(marked),
                makespan_s=max(finishes), worker_finish_s=finishes,
                mean_worker_finish_s=statistics.mean(finishes),
                mean_ttft_s=statistics.mean(ttfts), median_ttft_s=statistics.median(ttfts),
                p95_ttft_s=ttfts[math.ceil(.95 * len(ttfts)) - 1],
                p99_ttft_s=ttfts[math.ceil(.99 * len(ttfts)) - 1], max_ttft_s=ttfts[-1],
                worker_of=worker_of)


def export(out, export_dir, summary, run_digest, payload):
    """Copy the small artifacts to the Windows workspace. result.json is written last."""
    if not export_dir:
        return
    target = Path(export_dir)
    raw = target / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for name in ("report.json", "check.json", "read_admission.check.json", "layer_check.json"):
        if (out / "run" / name).exists():
            shutil.copyfile(out / "run" / name, raw / name)
    if (out / "harness_diff.json").exists():
        shutil.copyfile(out / "harness_diff.json", raw / "harness_diff.json")
    if summary:
        write_json(raw / "worker_of.json", summary["worker_of"])
    write_json(target / "result.json", payload)


def run(spec_path):
    spec = read_json(spec_path)
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=False)
    started = time.time()
    write_json(out / "status.json", dict(status="running", started=started))
    python = Path(spec.get("python") or DV / ".venv" / "bin" / "python")
    harness = None
    summary = None
    try:
        input_dir = prepare_input(spec, out, python)
        config_path = input_dir / "config.json"
        config = read_json(config_path)
        merge_policy(config, spec.get("policy"), int(spec.get("max_tier") or 0))
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        harness = build_harness(spec, out, python)
        replay(spec, harness, out, python, input_dir)
        summary = summarize(out / "run", Path(spec["bench_dir"]), spec.get("arm"))
        run_digest = digest(out / "run", Path(spec["bench_dir"]), summary)
        payload = dict(status="done", started=started, finished=time.time(),
                       summary={k: v for k, v in summary.items() if k != "worker_of"}, digest=run_digest)
        write_json(out / "result.json", payload)
        export(out, spec.get("export_dir"), summary, run_digest, payload)
        write_json(out / "status.json", dict(status="done", finished=time.time()))
        print(json.dumps(dict(run_id=spec.get("run_id"), **payload["summary"])), flush=True)
        return 0
    except Exception as error:  # noqa: BLE001 - every failure must reach the orchestrator
        if harness is not None:
            kill_run_processes(harness, out)
        text = "%s: %s" % (type(error).__name__, error)
        (out / "error.log").write_text(text + "\n" + traceback.format_exc())
        log = out / "run.log"
        tail = log.read_text(errors="replace")[-3000:] if log.exists() else ""
        payload = dict(status="failed", started=started, finished=time.time(), error=text, log_tail=tail)
        write_json(out / "result.json", payload)
        write_json(out / "status.json", dict(status="failed", error=text))
        export(out, spec.get("export_dir"), summary, None, payload)
        print(text, file=sys.stderr, flush=True)
        return 1


def base_manifest(path):
    """Hash a harness built exactly like the entry's tfs arm; list the files holding port literals."""
    with tempfile.TemporaryDirectory(prefix="tuning-base-") as tmp:
        tmp = Path(tmp)
        (tmp / "input").mkdir()
        harness = build_harness(dict(arm="tfs"), tmp, DV / ".venv" / "bin" / "python")
        hashes = tree_hashes(harness)
        ports = [rel for rel in hashes if rel.endswith(".py")
                 and PORT_RE.search((harness / rel).read_text(errors="replace"))]
        write_json(path, dict(harness=hashes, port_files=ports))
        return ports


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", nargs="?")
    parser.add_argument("--base-manifest")
    args = parser.parse_args()
    if args.base_manifest:
        print(json.dumps(dict(base_manifest=args.base_manifest, port_files=base_manifest(args.base_manifest))))
        return 0
    if not args.spec:
        parser.error("spec json or --base-manifest is required")
    return run(args.spec)


if __name__ == "__main__":
    sys.exit(main())
