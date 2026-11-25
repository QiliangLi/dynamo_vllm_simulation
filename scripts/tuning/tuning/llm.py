"""Running one LLM role task through opencode, unattended (section 4).

Each task gets a private agent definition (model, writable paths, shell
allowance), a task book on disk, a wall-clock limit and a record of what it
spent. The orchestrator validates the task's output files itself; this module
only reports what the agent did.
"""
import json
import subprocess
import threading
import time
from pathlib import Path

from . import config, roles

_semaphore = threading.Semaphore(3)
TAIL = 4000


def _kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


def run_task(workspace, task_id, role, task_md, write_paths, shell, model=None, session=None,
             timeout_min=None):
    """Run one agent. Returns a dict with status, text, tokens, steps, session_id, duration_s, error."""
    workspace = Path(workspace)
    agent = "%s-%s" % (role, task_id)
    roles.write_agent(workspace, agent, role, write_paths, shell)
    limit = (timeout_min or config.TIMEOUT_MIN[role]) * 60
    cmd = ["cmd.exe", "/d", "/c", "opencode", "run", "--agent", agent, "--format", "json", "--auto",
           "--archived", "--title", "tuning-%s" % task_id]
    if session:
        cmd += ["--session", session]
    cmd += ["--model", model or config.MODELS[role]]
    cmd.append("你的任务书在 %s。先完整读它，然后严格按它执行。" % task_md)

    started = time.time()
    result = dict(task_id=task_id, role=role, model=model or config.MODELS[role], status="error", text="",
                  tokens=dict(input=0, output=0, reasoning=0, cache_read=0, cache_write=0), steps=0,
                  tool_calls=0, session_id=None, duration_s=0.0, error="")
    texts, err_tail = [], []
    state = dict(session=None)

    def consume(stream, is_err):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if is_err:
                err_tail.append(line[-300:])
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            part = event.get("part") or {}
            kind = part.get("type")
            if event.get("sessionID"):
                state["session"] = event["sessionID"]
            if event.get("type") == "error":
                result["error"] = str(part.get("message") or event)[:500]
            if kind == "text":
                texts.append(part.get("text") or "")
            elif kind == "tool":
                result["tool_calls"] += 1
            elif kind == "step-finish":
                result["steps"] += 1
                tokens = part.get("tokens") or {}
                result["tokens"]["input"] += tokens.get("input") or 0
                result["tokens"]["output"] += tokens.get("output") or 0
                result["tokens"]["reasoning"] += tokens.get("reasoning") or 0
                result["tokens"]["cache_read"] += (tokens.get("cache") or {}).get("read") or 0
                result["tokens"]["cache_write"] += (tokens.get("cache") or {}).get("write") or 0

    with _semaphore:
        try:
            proc = subprocess.Popen(cmd, cwd=str(workspace), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL)
        except OSError as error:
            result["error"] = "cannot start opencode: %s" % error
            roles.remove_agent(workspace, agent)
            return result
        readers = [threading.Thread(target=consume, args=(proc.stdout, False), daemon=True),
                   threading.Thread(target=consume, args=(proc.stderr, True), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            code = proc.wait(timeout=limit)
            result["status"] = "ok" if code == 0 else "error"
            if code != 0 and not result["error"]:
                result["error"] = "opencode exited with %d" % code
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            proc.wait(timeout=30)
            result["status"] = "timeout"
            result["error"] = "exceeded %d minutes" % (limit // 60)
        finally:
            for reader in readers:
                reader.join(timeout=10)
            roles.remove_agent(workspace, agent)
    result["text"] = "\n".join(texts).strip()
    result["session_id"] = state["session"]
    result["duration_s"] = round(time.time() - started, 1)
    if result["status"] != "ok" and not result["error"]:
        result["error"] = " | ".join(err_tail[-5:])[-600:] or "no output"
    if result["status"] == "ok" and result["steps"] == 0:
        result.update(status="error", error="agent produced no steps")
    folder = workspace / "tasks" / task_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "llm.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "output.md").write_text(result["text"] + "\n", encoding="utf-8")
    return result


def tokens_total(tokens):
    return sum(tokens.values())
