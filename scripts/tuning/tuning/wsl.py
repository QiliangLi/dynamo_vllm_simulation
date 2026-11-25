"""Talking to the WSL side from the Windows orchestrator.

Every call goes through ``call``: one ``wsl.exe`` process, a bash script on
stdin, UTF-8 output, and a serialization lock. WSL is fragile while the virtual
machine is starting or stopping, and it cannot start at all when the host runs
out of memory, so callers get a classified failure instead of an exception and
are expected to retry later.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

DISTRO = "Ubuntu-24.04"
WSL_ROOT = "/root/tuning"
REPO = WSL_ROOT + "/repo"
PY = "/root/tuning/repo/dynamo_vllm_cpu_sim/.venv/bin/python"
RUNNER = "/root/tuning/repo/scripts/tuning/wsl/run_tuning.py"
BASE_MANIFEST = "/root/tuning/base_manifest.json"
PORT_STEP = 100

WSL_UTF8 = "1"
_lock = threading.Lock()


def to_wsl(path):
    """D:\\a\\b or D:/a/b -> /mnt/d/a/b"""
    text = str(Path(path).resolve())
    drive, rest = text[0].lower(), text[2:]
    return "/mnt/%s%s" % (drive, rest.replace("\\", "/"))


def call(script, timeout=180):
    """Run a bash script in WSL. Returns (rc, stdout, stderr, reason).

    The script goes in as UTF-8 bytes: text mode on Windows would turn every
    newline into CRLF, which bash does not accept.
    """
    env = dict(os.environ, WSL_UTF8=WSL_UTF8)
    with _lock:
        try:
            proc = subprocess.run(["wsl.exe", "-d", DISTRO, "--", "bash", "-s"], input=script.encode("utf-8"),
                                  capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return 124, "", "", "timeout"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    reason = ""
    if proc.returncode != 0:
        if "ERROR_NO_SYSTEM_RESOURCES" in err or "OUTOFMEMORY" in err:
            reason = "host_out_of_memory"
        elif "E_UNEXPECTED" in err or "NETNAME_DELETED" in err or "HCS" in err:
            reason = "wsl_unavailable"
        elif not out.strip():
            reason = "wsl_unavailable"
        else:
            reason = "script_failed"
    return proc.returncode, out, err, reason


def health():
    """Cheap preflight before launching a run."""
    script = """
echo __health__
test -x %s && echo venv_ok || echo venv_missing
test -f %s && echo manifest_ok || echo manifest_missing
test -f %s && echo runner_ok || echo runner_missing
awk '/MemAvailable/{printf "mem_avail_mb=%%d\\n", $2/1024}' /proc/meminfo
df -Pk /root | awk 'NR==2{printf "disk_avail_gb=%%d\\n", $4/1048576}'
""" % (PY, BASE_MANIFEST, RUNNER)
    rc, out, err, reason = call(script, timeout=120)
    info = dict(ok=False, reason=reason, mem_avail_mb=0, disk_avail_gb=0, raw=out[-400:])
    if rc != 0 or "__health__" not in out:
        return info
    for key in ("venv_ok", "manifest_ok", "runner_ok"):
        if key not in out:
            info["reason"] = key.replace("_ok", "_missing")
            return info
    info["mem_avail_mb"] = int(_last(out, "mem_avail_mb=(\\d+)"))
    info["disk_avail_gb"] = int(_last(out, "disk_avail_gb=(\\d+)"))
    if info["mem_avail_mb"] < 3500:
        info["reason"] = "wsl_low_memory"
        return info
    if info["disk_avail_gb"] < 30:
        info["reason"] = "wsl_low_disk"
        return info
    info.update(ok=True, reason="")
    return info


def _last(text, pattern):
    found = re.findall(pattern, text)
    return found[-1] if found else "0"


def launch(run_id, spec_path_on_windows):
    """Start one run detached inside WSL. The runner reports through its export directory."""
    spec = to_wsl(spec_path_on_windows)
    script = """
set -u
mkdir -p %s/runs %s/tmp
cd %s
setsid nohup %s %s %s > %s/runs/%s.log 2>&1 < /dev/null &
sleep 2
if pgrep -f "run_tuning.py .*%s" > /dev/null; then echo launched; else echo not_launched; tail -n 5 %s/runs/%s.log; fi
""" % (WSL_ROOT, WSL_ROOT, REPO, PY, RUNNER, spec, WSL_ROOT, run_id, run_id, WSL_ROOT, run_id)
    rc, out, err, reason = call(script, timeout=120)
    lines = out.strip().splitlines()
    if lines and lines[0] == "launched":
        return True, ""
    return False, (" ".join([reason] + lines) + " " + err.strip()[-200:])[-700:]


def kill(run_id):
    """Stop a run and everything started inside its harness."""
    script = """
cd %s
pkill -f "run_tuning.py .*%s" 2>/dev/null
%s - <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "%s")
import run_tuning
base = Path("%s/runs/%s")
if base.exists():
    run_tuning.kill_run_processes(base / "harness", base)
print("killed")
PYEOF
""" % (REPO, run_id, PY, str(Path(RUNNER).parent).replace("\\", "/"), WSL_ROOT, run_id)
    rc, out, err, reason = call(script, timeout=120)
    return rc == 0, out.strip()[-300:] or reason


def probe(run_id):
    """What WSL knows about a run that has not reported yet."""
    script = """
echo __probe__
if pgrep -f "run_tuning.py .*%s" > /dev/null; then echo alive; else echo gone; fi
ls %s/runs/%s 2>/dev/null | tr '\\n' ' '; echo
tail -n 12 %s/runs/%s.log 2>/dev/null
""" % (run_id, WSL_ROOT, run_id, WSL_ROOT, run_id)
    rc, out, err, reason = call(script, timeout=60)
    return out[-1500:] if rc == 0 else "probe failed: %s" % (reason or err[-300:])


def candidate_tests(cand_dir_on_windows, max_tier, timeout=420):
    """Build a harness with the candidate overlay and run its unit tests in WSL."""
    source = to_wsl(cand_dir_on_windows)
    work = "%s/tmp/test_%s" % (WSL_ROOT, Path(cand_dir_on_windows).name)
    script = """
set -u
rm -rf %(work)s && mkdir -p %(work)s/input
[ -d %(src)s/tests ] || { echo "NO_TESTS"; exit 0; }
cd %(repo)s
%(py)s - <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "%(wsl)s")
import run_tuning
work = Path("%(work)s")
harness = run_tuning.build_harness(dict(arm="tfs", overlay="%(src)s/overlay", base_manifest="%(manifest)s",
                                        max_tier=%(tier)d), work, "%(py)s")
print("HARNESS_OK", harness)
PYEOF
rc=$?
[ $rc -eq 0 ] || { echo "HARNESS_FAILED"; exit 0; }
mkdir -p %(work)s/harness/tuning_tests && cp -r %(src)s/tests/. %(work)s/harness/tuning_tests/
cd %(work)s/harness && timeout %(timeout)d %(py)s -m unittest discover -s tuning_tests -p "test_*.py" 2>&1 | tail -n 80
echo "UNITTEST_RC=${PIPESTATUS[0]}"
""" % dict(work=work, src=source, repo=REPO, py=PY, wsl=str(Path(RUNNER).parent).replace("\\", "/"),
           manifest=BASE_MANIFEST, tier=int(max_tier), timeout=int(timeout))
    rc, out, err, reason = call(script, timeout=timeout + 120)
    text = out if rc == 0 else "wsl call failed: %s %s" % (reason, err[-300:])
    if "NO_TESTS" in text:
        return "missing", "候选没有 tests/ 目录"
    if "HARNESS_FAILED" in text or "HARNESS_OK" not in text:
        return "error", text[-3000:]
    passed = "UNITTEST_RC=0" in text
    return ("pass" if passed else "fail"), text[-6000:]


def cleanup_runs(keep):
    """Delete finished WSL run directories that nobody needs any more."""
    keep = " ".join(sorted(keep))
    script = """
cd %(root)s/runs 2>/dev/null || exit 0
for d in *; do
  [ -d "$d" ] || continue
  case " %(keep)s " in *" $d "*) continue ;; esac
  [ -f "$d/result.json" ] || continue
  pgrep -f "run_tuning.py .*$d" > /dev/null && continue
  rm -rf "$d" "$d.log"
done
echo done; du -sh %(root)s/runs 2>/dev/null | cut -f1
""" % dict(root=WSL_ROOT, keep=keep)
    rc, out, err, reason = call(script, timeout=600)
    return out.strip().splitlines()[-1] if rc == 0 else reason


def disk_used():
    rc, out, err, reason = call("du -sh %s/runs 2>/dev/null | cut -f1" % WSL_ROOT, timeout=120)
    return out.strip() if rc == 0 else "?"
