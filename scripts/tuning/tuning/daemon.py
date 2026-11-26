"""The orchestrator core: durable state, the run queue, stage 0 and control.

One object, ``Daemon``, owns the state document and advances it in small steps.
Every step is idempotent: after a crash the next call looks at the state and the
files on disk and continues where things stopped. The daemon never stops on its
own. It stops when the user asks (a STOP file), and it suspends itself only for
safety reasons that need a human.
"""
import hashlib
import os
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import config, evaluator, llm, wsl
from .store import Store, read_json, write_json

FROZEN_FILES = ("evaluator.py", "config.py", "scope.py", "gates.py")
POLL_S = 5
PORT_FILES = ["scripts/benchmark_runtime.py", "scripts/run_tfs_stack.py",
              "scripts/validate_prefill_integration.py", "sim/stack/admission_service.py"]
STRESS_ROOT = wsl.WSL_ROOT + "/stress"
INFRA_TAGS = ("baseline", "control", "probe", "stress_baseline", "final")
WSL_REASONS = ("host_out_of_memory", "wsl_unavailable", "not_launched", "timed out", "wsl_low")
CANDIDATE_TAGS = ("smoke", "screen", "confirm", "attack", "final")
RESTART_EXIT = 3   # a planned restart: the supervisor starts the daemon again a minute later


def queued_key(tag, workload):
    """Key of a candidate's outstanding-replay counter. The state is JSON: a tuple key
    here crashed the daemon the first time a candidate's replay was collected."""
    return "%s|%s" % (tag, workload)


def failure_reason(result):
    """Why a replay failed, down to the exception that stopped it.

    The runner reports ``replay exited with 1``; the last line of its log tail names the error
    (a candidate's first real failure read ``KeyError`` in the checker's load-observation join).
    The next generation reads these reasons, so they have to say what broke.
    """
    error = str(result.get("error", "")).strip()
    lines = [line.strip() for line in str(result.get("log_tail", "")).splitlines() if line.strip()]
    tail = next((line for line in reversed(lines) if line not in error), "")
    return ("%s | %s" % (error, tail[-240:])) if tail else error


STRESS_BASELINE_RUNS = 3


def now():
    return time.time()


def iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def frozen_hashes_of(package_dir):
    return {name: hashlib.sha256((Path(package_dir) / name).read_bytes()).hexdigest() for name in FROZEN_FILES}


def frozen_hashes():
    return frozen_hashes_of(Path(__file__).parent)


def initial_state(base_commit):
    return dict(
        version=1, created=now(), base_commit=base_commit, frozen=frozen_hashes(),
        phase="stage0", resume_phase="stage0", needs_human="",
        parallel=1, stage0=dict(step="baseline", level_tested=1, armed=False),
        queue=[], active={}, run_seq=0, runs_total=0, runs_ok=0, runs_failed=0, light_failed_streak=0,
        since_control=0, drift_strikes=0, baseline={}, stress_baseline={}, stress={},
        candidates={}, champion=None, champion_history=[], gen=dict(n=0, step="open"),
        map={}, ladder=0, stagnation=0, tasks={},
        meta=dict(lessons_md="", hypotheses=[], proposer_hints="", redteam_hints="",
                  allocation=dict(llm=2, param=2), session=None, amendments=[]),
        tokens={}, crossval=dict(gen=0, used=0), env={}, alerts=[], final={}, dashboard={},
        runs_by_kind=dict(llm=0, param=0))


def open_tier(state):
    """Highest tier-2 scope tier currently open, from the ladder rung (section 2)."""
    rung = int(state.get("ladder") or 0)
    return max([tier for tier, at in config.TIER2_RUNG.items() if rung >= at] or [0])


class Daemon:
    def __init__(self, root):
        self.ws = Path(root).resolve()
        self.store = Store(self.ws)
        self.state = self.store.load()
        for name in ("candidates", "runs", "digest", "meta", "tasks", "control", "dashboard", "reviews",
                     "judgements", "audits", "crossval", "generations"):
            (self.ws / name).mkdir(parents=True, exist_ok=True)
        self.threads = {}
        self._errors = 0
        self._health = (0.0, None)
        self._keep = None
        self._probe_at = 0.0
        self._wsl_lock = threading.Lock()
        self.base_dir = self.ws / "base"

    # ------------------------------------------------------------ bookkeeping

    def save(self):
        self.state["last_beat"] = now()
        self.store.save(self.state)

    def log(self, event, **fields):
        self.store.append(dict(fields, kind=event))

    def health(self):
        if self._health[1] is None or now() - self._health[0] > 30:
            self._health = (now(), wsl.health())
            self.state["env"]["health"] = dict(ok=self._health[1]["ok"], reason=self._health[1]["reason"], at=iso(now()))
        return self._health[1]

    def control(self):
        control = self.ws / "control"
        if (control / "STOP").exists():
            return "stop"
        if (control / "PAUSE").exists():
            return "pause"
        return "go"

    def alert(self, text, level="info"):
        self.state["alerts"] = (self.state.get("alerts") or [])[-19:] + [dict(at=iso(now()), text=text, level=level)]

    def needs_human(self, reason):
        self.state.update(phase="needs_human", needs_human=reason)
        self.log("needs_human", reason=reason)
        (self.ws / "NEEDS_HUMAN.md").write_text(
            "# 需要人工处理\n\n时间：%s\n\n原因：%s\n\n处理后：删除本文件和 control/PAUSE，再双击「继续」。\n"
            % (iso(now()), reason), encoding="utf-8")

    def verify_frozen(self):
        """Section 11. Refuse to evaluate anything if a frozen file changed since start-up."""
        current = frozen_hashes()
        pinned = self.state.get("frozen") or {}
        if pinned and pinned != current:
            changed = sorted(k for k in current if pinned.get(k) != current[k])
            raise SystemExit("frozen files changed: %s (planned upgrade: python tune.py control unfreeze)" % changed)

    def register_frozen_at_start(self):
        """Honour control/UNFREEZE before the check: the request is the operator's explicit consent."""
        path = self.ws / "control" / "UNFREEZE"
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        before = self.state.get("frozen")
        self.state["frozen"] = frozen_hashes()
        self.log("unfreeze", at_start=True, before=before, after=self.state["frozen"])
        return True

    def planned_exit(self):
        """Why the daemon should stop now, so that the supervisor starts it again a minute later.

        Returns 'unfreeze', 'restart' or None. Both requests are files in control/ and wait for a moment
        when no model task is running: killing a task loses its work (a proposal runs for half an hour
        or more). The loop asks here before it enters any step that could start one.

        ``unfreeze`` registers the frozen files as they are on disk. The daemon does it itself rather
        than a command writing the state file, because this process overwrites that file every few
        seconds with what it holds in memory.
        """
        if any(thread.is_alive() for thread in self.threads.values()):
            return None
        control = self.ws / "control"
        if (control / "UNFREEZE").exists():
            (control / "UNFREEZE").unlink(missing_ok=True)
            before = self.state.get("frozen")
            self.state["frozen"] = frozen_hashes()
            self.log("unfreeze", before=before, after=self.state["frozen"])
            return "unfreeze"
        if (control / "RESTART").exists():
            (control / "RESTART").unlink(missing_ok=True)
            return "restart"
        return None

    # ------------------------------------------------------------ the loop

    def loop(self):
        self.register_frozen_at_start()
        self.verify_frozen()
        self.log("daemon_start", pid=os.getpid())
        (self.ws / "daemon.pid").write_text(str(os.getpid()))
        while True:
            try:
                command = self.control()
                phase = self.state["phase"]
                if command == "stop" and phase not in ("stopping", "stopped"):
                    self.state["phase"] = "stopping"
                    self.log("stop_requested")
                phase = self.state["phase"]
                if phase == "stopped":
                    break
                if command == "pause" and phase not in ("paused", "stopping", "stopped", "needs_human"):
                    self.state.update(resume_phase=phase, phase="paused")
                    self.log("paused", phase=phase)
                elif command == "go" and phase == "paused":
                    self.state["phase"] = self.state.get("resume_phase") or "run"
                    self.log("resumed", phase=self.state["phase"])
                phase = self.state["phase"]
                reason = None if phase == "stopping" else self.planned_exit()
                if reason:
                    # A task that has just finished is already recorded; the next daemon picks its result up.
                    self.save()
                    self.log("planned_restart", reason=reason, phase=phase, gen=self.state["gen"]["n"],
                             step=self.state["gen"].get("step"))
                    raise SystemExit(RESTART_EXIT)
                if phase == "stopping":
                    from .search import Finalizer
                    Finalizer(self).step()
                elif phase not in ("paused", "needs_human"):
                    self.pump_runs()
                    if self.state["phase"] == "stage0":
                        self.stage0()
                    elif self.state["phase"] == "run":
                        from .search import Search
                        Search(self).step()
            except SystemExit:
                raise
            except Exception as error:  # noqa: BLE001 - the loop must survive any single failure
                self._errors += 1
                self.log("daemon_error", error=repr(error), trace=traceback.format_exc()[-3000:], streak=self._errors)
                if self._errors >= 40:
                    self.needs_human("连续 %d 次内部错误：%s" % (self._errors, repr(error)[:300]))
                time.sleep(min(300, 10 * self._errors))
            else:
                self._errors = 0
            self.reap_threads()
            try:
                self.keepalive()
                self.check_liveness()
                self.take_requests()
                if now() - self.state["env"].get("disk_at", 0) > 900:
                    self.state["env"]["disk_at"] = now()
                    self.state["env"]["disk"] = wsl.disk_used()
            except Exception as error:  # noqa: BLE001
                self.log("housekeeping_error", error=repr(error))
            try:
                from .report import Dashboard
                Dashboard(self).render()
            except Exception as error:  # noqa: BLE001
                self.log("dashboard_error", error=repr(error), trace=traceback.format_exc()[-1500:])
            try:
                self.save()
            except Exception as error:  # noqa: BLE001 - keep the in-memory state and try again next turn
                self.log("state_save_error", error=repr(error))
                time.sleep(20)
            time.sleep(POLL_S)
        self.log("daemon_stop")

    # ------------------------------------------------------------ LLM threads

    def start_task(self, task_id, role, task_md, write_paths, shell, **options):
        """Launch one agent in the background. ``state['tasks'][task_id]`` tracks it."""
        record = self.state["tasks"].setdefault(task_id, dict(attempts=0))
        record.update(status="running", role=role, started=now(), attempts=record["attempts"] + 1, error="")

        def work():
            try:
                outcome = llm.run_task(self.ws, task_id, role, task_md, write_paths, shell, **options)
            except Exception as error:  # noqa: BLE001
                outcome = dict(status="error", error=repr(error), tokens={}, steps=0, tool_calls=0,
                               session_id=None, duration_s=0.0, text="")
            record = self.state["tasks"].get(task_id, {})
            record.update(status=outcome["status"], finished=now(), duration_s=outcome["duration_s"],
                          error=outcome.get("error", ""), session_id=outcome.get("session_id"),
                          steps=outcome["steps"], tool_calls=outcome["tool_calls"])
            # Section 11. Nothing an agent wrote outside its task may survive it.
            try:
                from . import guard
                undone = guard.restore(self.ws)
                if undone:
                    record["guard_restored"] = [path for _, path in undone][:30]
                    self.log("guard_restored", task=task_id, paths=record["guard_restored"])
                    self.alert("任务 %s 改动了受保护的内容，已还原：%s" % (task_id, "、".join(record["guard_restored"][:5])), level="warn")
                self.refresh_digests()
                own = next((p.split("/")[1] for p in write_paths if p.startswith("candidates/")), None)
                for other in guard.tampered_candidates(self.state, self.ws, own):
                    if self.state["candidates"][other].get("status") not in ("champion",):
                        self.state["candidates"][other].update(status="rejected", reason="它的文件被任务 %s 改动" % task_id)
                        self.alert("候选 %s 的文件被任务 %s 改动，已作废" % (other, task_id), level="warn")
            except Exception as error:  # noqa: BLE001
                self.log("guard_error", task=task_id, error=repr(error))
            family = "kimi" if role in config.NON_GLM_ROLES else "glm"
            bucket = self.state["tokens"].setdefault(family, dict(input=0, output=0, reasoning=0, calls=0))
            for key in ("input", "output", "reasoning"):
                bucket[key] += (outcome.get("tokens") or {}).get(key, 0)
            bucket["calls"] += 1
            by_role = self.state["tokens"].setdefault("by_role", {})
            role_bucket = by_role.setdefault(role, dict(input=0, output=0, calls=0))
            for key in ("input", "output"):
                role_bucket[key] += (outcome.get("tokens") or {}).get(key, 0)
            role_bucket["calls"] += 1
            self.log("llm_task", task=task_id, role=role, status=outcome["status"],
                     duration_s=outcome["duration_s"], tokens=outcome.get("tokens"), error=outcome.get("error", ""))
            self.save()

        thread = threading.Thread(target=work, daemon=True)
        self.threads[task_id] = thread
        thread.start()

    def refresh_digests(self):
        """Rewrite the two digests the agents read, from the state.

        ``digest/baseline.md`` and ``digest/champion.md`` are the daemon's own: the baseline grows
        with every control run, the champion appears at the first promotion. They are not tracked,
        so the guard's revert leaves them alone, and rewriting them here is what overwrites anything
        an agent may have put into them. (Tracked, the guard reverted the champion to "none" after
        the next task.)
        """
        from .report import write_baseline_digest, write_champion_digest
        if any(bucket.get("light") for bucket in self.state["baseline"].values()):
            write_baseline_digest(self)
        write_champion_digest(self)

    def reap_threads(self):
        for task_id, thread in list(self.threads.items()):
            if not thread.is_alive():
                del self.threads[task_id]

    def task_state(self, task_id):
        """'running' only while a live thread owns the task; a stale record after a restart is 'lost'."""
        record = self.state["tasks"].get(task_id)
        if not record:
            return "none"
        thread = self.threads.get(task_id)
        if record.get("status") == "running":
            return "running" if thread and thread.is_alive() else "lost"
        return record["status"]

    # ------------------------------------------------------------ keeping WSL alive

    def keepalive(self):
        """Hold one WSL session open for as long as the daemon runs.

        WSL stops a distribution that has no attached session, whatever runs inside it, and
        that kills the replay in progress. A sleeping session costs nothing and keeps it up.
        """
        if self._keep is not None and self._keep.poll() is None:
            return
        if self._keep is not None:
            self.log("wsl_keepalive_restarted", code=self._keep.returncode)
        elif self._keep is None and not getattr(self, "_keep_cleaned", False):
            self._keep_cleaned = True
            self.drop_orphan_sessions()
        try:
            self._keep = subprocess.Popen(wsl.KEEPALIVE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:  # noqa: BLE001
            self._keep = None
            self.log("wsl_keepalive_failed", error=repr(error))

    def drop_orphan_sessions(self):
        """Clean up after a previous daemon instance.

        Keep-alive WSL sessions it left would pile up after restarts. An opencode agent it started
        would keep writing into a candidate directory that a relaunched task is writing too.
        """
        orphans = (
            ("Name='wsl.exe'", "*sleep*infinity*"),
            ("Name='opencode.exe'", "*--title tuning-*"),
        )
        for selector, pattern in orphans:
            command = ("Get-CimInstance Win32_Process -Filter \"%s\" | Where-Object { "
                       "$_.CommandLine -like '%s' -and -not (Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue) } | "
                       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % (selector, pattern))
            try:
                subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def check_liveness(self):
        """A replay whose runner vanished (WSL restarted, process killed) must not wait for its timeout."""
        active = self.state["active"]
        if not active or now() - self._probe_at < 60:
            return
        self._probe_at = now()
        with self._wsl_lock:
            alive = wsl.running_runs()
        if alive is None:
            return
        for run_id in sorted(active):
            if run_id in alive or now() - active[run_id]["started"] < 45:
                continue
            if (self.ws / "runs" / run_id / "result.json").exists():
                continue
            del active[run_id]
            self.alert("一轮回放的运行器消失了（WSL 重启或进程被杀），已记失败并补跑：%s" % run_id, level="warn")
            self.collect(run_id, dict(status="failed", error="runner vanished: WSL restarted or the process was killed"))

    # ------------------------------------------------------------ helper requests

    def take_requests(self):
        """Smoke replays that an agent asked for through ``python tune.py smoke``."""
        folder = self.ws / "requests"
        if not folder.exists():
            return
        for path in sorted(folder.glob("smoke_*.json")):
            if path.name.endswith(".started.json"):
                continue  # the receipt this method writes for the waiting agent
            try:
                request = read_json(path)
            except ValueError:
                path.unlink()
                continue
            source = self.ws / "candidates" / request["cand"]
            run_id = self.new_run(request["workload"], "candidate", "smoke", cand=request["cand"], gen=self.state["gen"]["n"],
                                  overlay=source / "overlay" if (source / "overlay").exists() else None,
                                  policy=request.get("policy"))
            write_json(path.with_name(path.stem + ".started.json"), dict(run=run_id))
            path.unlink()
            # An agent is waiting on this one: in the search phase it goes ahead of the queue.
            # Stage 0 keeps its own order so its measurements stay untouched.
            queue = self.state["queue"]
            if self.state["phase"] == "run" and run_id in queue:
                queue.remove(run_id)
                queue.insert(0, run_id)

    # ------------------------------------------------------------ run queue

    def new_run(self, workload, arm, tag, cand=None, gen=0, stress=None, workers=4, overlay=None,
                policy=None, max_tier=0, exclusive=False, group=None, timeout_s=1500):
        """Queue one replay. Returns the run id; the launch happens in pump_runs."""
        self.state["run_seq"] += 1
        run_id = "r%05d" % self.state["run_seq"]
        folder = self.ws / "runs" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        spec = dict(run_id=run_id, workload=workload, arm=arm, tag=tag, cand=cand, gen=gen, stress=stress,
                    exclusive=bool(exclusive), group=group, created=now(), timeout_s=timeout_s)
        if stress:
            root = "%s/%s" % (STRESS_ROOT, stress)
            remote = dict(input_dir=root + "/input", bench_dir=root + "/bench")
        else:
            bench = config.WORKLOADS[workload]["bench"]
            remote = dict(bench=bench, bench_dir="%s/benchmark/%s" % (wsl.REPO, bench))
        remote.update(
            run_id=run_id, arm=arm, out="%s/runs/%s" % (wsl.WSL_ROOT, run_id), export_dir=wsl.to_wsl(folder),
            base_manifest=wsl.BASE_MANIFEST, port_files=PORT_FILES, port_offset=0, workers=int(workers),
            max_tier=int(max_tier), timeout_s=int(timeout_s), overlay=wsl.to_wsl(overlay) if overlay else None,
            policy=policy or None)
        spec["remote"] = remote
        write_json(folder / "spec.json", spec)
        self.state["queue"].append(run_id)
        self.state["runs_total"] += 1
        if cand:
            kind = (self.state["candidates"].get(cand) or {}).get("kind", "llm")
            self.state["runs_by_kind"][kind if kind in ("llm", "param") else "llm"] += 1
        self.log("run_queued", run=run_id, workload=workload, arm=arm, tag=tag, cand=cand)
        return run_id

    def pump_runs(self):
        """Collect what finished, then launch what may run now."""
        state = self.state
        active = state["active"]
        for run_id in sorted(active):
            result_path = self.ws / "runs" / run_id / "result.json"
            if result_path.exists():
                try:
                    result = read_json(result_path)
                except ValueError:
                    continue
                del active[run_id]
                self.collect(run_id, result)
            elif now() - active[run_id]["started"] > active[run_id]["timeout"]:
                del active[run_id]
                with self._wsl_lock:
                    wsl.kill(run_id)
                self.collect(run_id, dict(status="failed", error="run timed out"))

        queue = state["queue"]
        while queue:
            if any(item.get("exclusive") for item in active.values()):
                return
            spec = read_json(self.ws / "runs" / queue[0] / "spec.json")
            launch, exclusive = [queue[0]], bool(spec.get("exclusive"))
            if spec.get("group"):
                launch = [rid for rid in queue
                          if (read_json(self.ws / "runs" / rid / "spec.json").get("group") == spec["group"])][:config.PARALLEL_MAX]
            level = int(state.get("parallel") or 1)
            if exclusive and active:
                return
            if spec.get("group") and active:
                return
            if not exclusive and not spec.get("group") and len(active) >= level:
                return
            health = self.health()
            if not health["ok"]:
                state["env"].setdefault("waiting_since", iso(now()))
                state["env"]["waiting_reason"] = health["reason"]
                return
            state["env"].pop("waiting_since", None)
            used = sorted(item["slot"] for item in active.values())
            for run_id in launch:
                queue.remove(run_id)
                slot = 0
                while slot in used:
                    slot += 1
                used.append(slot)
                path = self.ws / "runs" / run_id / "spec.json"
                spec = read_json(path)
                spec["remote"]["port_offset"] = slot * wsl.PORT_STEP
                spec["slot"] = slot
                write_json(path, spec)
                remote_path = path.with_name("remote.json")
                write_json(remote_path, spec["remote"])
                with self._wsl_lock:
                    ok, detail = wsl.launch(run_id, remote_path)
                if ok:
                    active[run_id] = dict(slot=slot, started=now(), timeout=spec["remote"]["timeout_s"] + 240,
                                          exclusive=exclusive, group=spec.get("group"), workload=spec["workload"],
                                          tag=spec["tag"], cand=spec.get("cand"))
                    self.log("run_started", run=run_id, slot=slot, workload=spec["workload"], tag=spec["tag"], cand=spec.get("cand"))
                elif any(key in str(detail) for key in WSL_REASONS if key != "not_launched"):
                    # WSL could not take it: keep it and wait for the environment.
                    queue.insert(0, run_id)
                    self.log("run_launch_failed", run=run_id, detail=str(detail)[:300])
                    return
                else:
                    # The runner died at once; twice means the run itself is broken.
                    failures = state.setdefault("launch_fail", {})
                    failures[run_id] = failures.get(run_id, 0) + 1
                    self.log("run_launch_failed", run=run_id, detail=str(detail)[:400])
                    if failures[run_id] < 2:
                        queue.insert(0, run_id)
                    else:
                        self.collect(run_id, dict(status="failed", error="runner exited at once: %s" % str(detail)[-600:]))

    def collect(self, run_id, result):
        """Account for one finished run everywhere it matters."""
        state = self.state
        spec = read_json(self.ws / "runs" / run_id / "spec.json")
        done = result.get("status") == "done"
        state["runs_ok" if done else "runs_failed"] += 1
        summary = result.get("summary") or {}
        self.log("run_finished", run=run_id, status=result.get("status"), workload=spec["workload"], tag=spec["tag"],
                 cand=spec.get("cand"), ttft=summary.get("mean_ttft_s"), makespan=summary.get("makespan_s"),
                 error=result.get("error", ""), detail="" if done else failure_reason(result)[-240:])
        state["dashboard"]["last_run"] = dict(run=run_id, workload=spec["workload"], tag=spec["tag"], cand=spec.get("cand"),
                                              status=result.get("status"), at=iso(now()), error=result.get("error", ""))
        cand = state["candidates"].get(spec.get("cand")) if spec.get("cand") else None
        if cand is not None and spec["tag"] in CANDIDATE_TAGS:
            # State is JSON on disk: keys must be strings.
            key = queued_key(spec["tag"], spec["workload"])
            cand.setdefault("queued", {})[key] = max(0, cand["queued"].get(key, 0) - 1)
        if not done:
            if spec["arm"] == "light" and spec["tag"] in INFRA_TAGS:
                state["light_failed_streak"] += 1
                # Baseline and stress runs are the reference for everything: replace a lost one, twice at most.
                key = "%s|%s|%s" % (spec["tag"], spec["workload"], spec.get("stress"))
                retries = state.setdefault("infra_retry", {})
                retries[key] = retries.get(key, 0) + 1
                if retries[key] <= 2 and spec["tag"] != "control":
                    self.new_run(spec["workload"], "light", spec["tag"], gen=spec.get("gen", 0), stress=spec.get("stress"),
                                 workers=spec["remote"].get("workers", 4), exclusive=spec.get("exclusive", False),
                                 group=spec.get("group"))
                if state["light_failed_streak"] >= 10:
                    self.needs_human("连续 10 轮默认策略运行失败：%s" % failure_reason(result)[:400])
            elif cand is not None:
                name = "%s|%s" % (spec["tag"], spec["workload"])
                cand["fail"][name] = cand.get("fail", {}).get(name, 0) + 1
                if cand["fail"][name] >= 2:
                    cand["run_failed"] = "%s %s：%s" % (spec["workload"], spec["tag"], failure_reason(result))
                else:
                    self.requeue(spec, cand["id"])
            return
        if spec["arm"] == "light":
            state["light_failed_streak"] = 0
        if spec["tag"] in ("baseline", "control") and not spec.get("stress"):
            bucket = state["baseline"].setdefault(spec["workload"], {}).setdefault(
                spec["arm"], dict(ttft=[], makespan=[], runs=[]))
            bucket["ttft"].append(summary["mean_ttft_s"])
            bucket["makespan"].append(summary["makespan_s"])
            bucket["runs"].append(run_id)
        elif spec["tag"] == "stress_baseline":
            light = state["stress_baseline"].setdefault(spec["stress"], dict(light=dict(ttft=[], makespan=[], runs=[])))["light"]
            light["ttft"].append(summary["mean_ttft_s"])
            light["makespan"].append(summary["makespan_s"])
            light["runs"].append(run_id)
        elif spec["tag"] == "final" and spec["arm"] == "light":
            final = state.setdefault("final", {}).setdefault("light", {}).setdefault(spec["workload"], dict(ttft=[], makespan=[]))
            final["ttft"].append(summary["mean_ttft_s"])
            final["makespan"].append(summary["makespan_s"])
        elif cand is not None and spec["tag"] in CANDIDATE_TAGS:
            runs = cand.setdefault("runs", {}).setdefault(spec["workload"], {})
            runs.setdefault(spec["tag"], []).append(dict(run=run_id, ttft=summary["mean_ttft_s"], makespan=summary["makespan_s"],
                                                         stress=spec.get("stress")))
        if spec["tag"] == "control":
            self.check_control(spec["workload"], summary["mean_ttft_s"], summary["makespan_s"])
        if spec["tag"] in ("screen", "confirm", "attack"):
            state["since_control"] += 1
            if state["since_control"] >= config.CONTROL_EVERY and state["phase"] == "run":
                state["since_control"] = 0
                workloads = list(config.WORKLOADS)
                self.new_run(workloads[state["runs_ok"] % len(workloads)], "light", "control", gen=state["gen"]["n"])

    def requeue(self, spec, cand_id):
        """One replacement for a failed candidate replay; a second failure stops the candidate."""
        folder = self.ws / "candidates" / cand_id
        cand = self.state["candidates"][cand_id]
        key = queued_key(spec["tag"], spec["workload"])
        cand.setdefault("queued", {})[key] = cand["queued"].get(key, 0) + 1
        self.new_run(spec["workload"], spec["arm"], spec["tag"], cand=cand_id, gen=spec.get("gen", cand.get("gen", 0)),
                     stress=spec.get("stress"), workers=spec["remote"].get("workers", 4),
                     overlay=folder / "overlay" if (folder / "overlay").exists() else None,
                     policy=spec["remote"].get("policy"), max_tier=spec["remote"].get("max_tier", 0),
                     exclusive=spec.get("exclusive", False), group=spec.get("group"))

    def check_control(self, workload, ttft, makespan):
        light = self.state["baseline"].get(workload, {}).get("light")
        if not light or len(light["ttft"]) < 3:
            return
        outside = evaluator.control_ok(dict(ttft=light["ttft"], makespan=light["makespan"]), ttft, makespan)
        if not outside:
            self.state["drift_strikes"] = 0
            return
        self.state["drift_strikes"] += 1
        self.alert("对照运行偏离基线 3 个标准差：%s（%s）" % (workload, "、".join(outside)), level="warn")
        self.log("drift", workload=workload, ttft=ttft, makespan=makespan, strikes=self.state["drift_strikes"])
        if self.state["drift_strikes"] >= 2:
            self.state["drift_strikes"] = 0
            self.state.setdefault("baseline_archives", []).append(dict(at=iso(now()), data=self.state["baseline"]))
            for workload in config.WORKLOADS:
                for _ in range(5):
                    self.new_run(workload, "light", "baseline", gen=self.state["gen"]["n"])
            self.alert("确认环境漂移，正在重测默认策略基线（25 轮）", level="warn")
            self.log("rebaseline")

    def busy(self):
        return bool(self.state["queue"] or self.state["active"])

    # ------------------------------------------------------------ stage 0

    def stage0(self):
        from .report import write_baseline_digest, write_bounds
        state, step = self.state, self.state["stage0"]
        # Each phase owns one flag. An earlier version reused ``armed`` for the baseline and the
        # probes, so a state written by it arrives here flagged as armed without a level.
        if step["step"] == "parallel" and "level" not in step:
            step["armed"] = False
        if step["step"] == "baseline":
            if not step.get("queued"):
                for arm, count in (("light", config.BASELINE_LIGHT_RUNS), ("tfs", config.BASELINE_TFS_RUNS)):
                    for index in range(count):
                        for workload in config.WORKLOADS:
                            self.new_run(workload, arm, "baseline")
                step["queued"] = True
                self.log("stage0_baseline_queued", light=config.BASELINE_LIGHT_RUNS, tfs=config.BASELINE_TFS_RUNS)
            enough = all(len(state["baseline"].get(w, {}).get("light", {}).get("ttft", [])) >= 5 for w in config.WORKLOADS)
            if enough and not self.busy():
                step["step"] = "parallel"
            return
        if step["step"] == "parallel":
            if not step.get("armed"):
                if step["level_tested"] >= config.PARALLEL_MAX:
                    self.decide_parallel()
                    return
                level = step["level_tested"] + 1
                # Two groups per level, and every core workload in each: the level is only as good as the
                # workload that reacts most to a neighbour, and that is not known before the probes run.
                for index in range(2):
                    for workload in config.WORKLOADS:
                        for slot in range(level):
                            self.new_run(workload, "light", "probe", group="probe%d_%d_%s" % (level, index, workload))
                step.update(armed=True, level=level)
                self.log("stage0_parallel_probe", level=level)
                return
            if self.busy():
                return
            step.update(armed=False, level_tested=step["level"])
            return
        if step["step"] == "stress":
            if not step.get("built"):
                ok, detail = self.build_stress()
                if not ok:
                    self.alert("压力负载没有生成：%s" % str(detail)[-200:], level="error")
                step.update(built=True, ok=ok)
            if step.get("ok"):
                # Three baseline runs per stress workload, asked for once. One that fails every
                # time simply ends without a baseline and is left out of the attack suite.
                asked = state.setdefault("stress_asked", {})
                for name, spec in config.STRESS_SPECS.items():
                    have = len(state["stress_baseline"].get(name, {}).get("light", {}).get("ttft") or [])
                    for _ in range(max(0, STRESS_BASELINE_RUNS - max(asked.get(name, 0), have))):
                        self.new_run(name, "light", "stress_baseline", stress=name, workers=spec.get("workers", 4))
                    asked[name] = max(STRESS_BASELINE_RUNS, asked.get(name, 0))
            if self.busy():
                return
            step["step"] = "bounds"
            return
        if step["step"] == "bounds":
            write_bounds(self)
            write_baseline_digest(self)
            state.update(phase="run", gen=dict(n=0, step="open"))
            self.log("stage0_done", parallel=state["parallel"], stress_built=state["stress"].get("built", False))

    def decide_parallel(self):
        """Replays may share the host up to the largest probe level that looks like the sequential baseline.

        A level counts only when every workload accepts it. One workload that reacts to a neighbour is
        enough to refuse it: every candidate would then be measured slower than the serial baseline on
        that workload (measured 2026-11-26: level 2 read 7.7% slower in TTFT and 3.1% in makespan on 408,
        nothing on mooncake and AgentX, 1.3% on 540). A level above a refused one is not usable either.
        """
        chosen, report = 1, []
        for level in range(2, config.PARALLEL_MAX + 1):
            runs = {}
            for path in sorted((self.ws / "runs").glob("r*/spec.json")):
                spec = read_json(path)
                if spec.get("tag") == "probe" and str(spec.get("group", "")).startswith("probe%d_" % level):
                    result_path = path.parent / "result.json"
                    if result_path.exists() and read_json(result_path).get("status") == "done":
                        runs.setdefault(spec.get("workload"), []).append(read_json(result_path)["summary"])
            rows, ok = [], chosen == level - 1
            for workload in config.WORKLOADS:
                items = runs.get(workload, [])
                row = dict(workload=workload, runs=len(items))
                if len(items) < 2:
                    row.update(accepted=False, why="样本不足或运行失败")
                else:
                    light = self.state["baseline"][workload]["light"]
                    ref_t, ref_m = evaluator.stats(light["ttft"]), evaluator.stats(light["makespan"])
                    t = evaluator.stats([r["mean_ttft_s"] for r in items])
                    m = evaluator.stats([r["makespan_s"] for r in items])
                    row.update(
                        ttft=t["mean"], makespan=m["mean"],
                        accepted=(abs(t["mean"] - ref_t["mean"]) <= 0.025 * ref_t["mean"]
                                  and abs(m["mean"] - ref_m["mean"]) <= 0.025 * ref_m["mean"]
                                  and t["sd"] <= 2 * max(ref_t["sd"], 1e-6) and m["sd"] <= 2 * max(ref_m["sd"], 1e-6)))
                ok = ok and row["accepted"]
                rows.append(row)
            report.append(dict(level=level, accepted=ok, workloads=rows))
            if ok:
                chosen = level
        self.state["parallel"] = chosen
        self.state["stage0"].update(step="stress", parallel_report=report)
        self.log("stage0_parallel_decided", parallel=chosen, report=report)

    def build_stress(self):
        with self._wsl_lock:
            rc, out, err, reason = wsl.call("cd %s && %s scripts/tuning/wsl/stress_gen.py --out %s\n"
                                            % (wsl.REPO, wsl.PY, STRESS_ROOT), timeout=2400)
        ok = rc == 0 and "STRESS_OK" in out
        self.state["stress"] = dict(built=ok, detail=(out + err)[-800:], at=iso(now()))
        self.log("stress_built", ok=ok, detail=(out + err)[-500:])
        return ok, (out + err)[-800:]
