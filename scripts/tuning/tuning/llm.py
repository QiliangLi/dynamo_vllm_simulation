"""Running one LLM role task through opencode, unattended (section 4).

Each task gets a private agent definition (model, writable paths, shell
allowance), a task book on disk, a wall-clock limit and a record of what it
spent. The orchestrator validates the task's output files itself; this module
only reports what the agent did.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from . import config, roles

_semaphore = threading.Semaphore(3)
TAIL = 4000
# Where opencode keeps its sessions, with the model of every assistant message. The workflow names a model
# per role, but opencode has a default of its own; what ran is what this database says ran.
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _model_key(model):
    model = model if isinstance(model, dict) else {}
    return "%s/%s#%s" % (model.get("providerID"), model.get("id"), model.get("variant") or "default")


def _connect(db_path):
    connection = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only = 1")
    return connection


def session_models(session_id, started, finished, db_path=None):
    """Which models opencode really ran one task on: {"<provider>/<id>#<variant>": assistant messages}.

    None when the session is unknown or the database cannot be read. A session may continue an earlier one
    (the meta-strategist keeps its session across generations), so only the assistant messages written
    while this task was running count.
    """
    path = Path(db_path) if db_path else OPENCODE_DB
    if not session_id or not path.exists():
        return None
    try:
        connection = _connect(path)
        rows = connection.execute("SELECT time_created, data FROM session_message WHERE session_id = ? AND type = 'assistant'",
                                  (session_id,)).fetchall()
        connection.close()
    except sqlite3.Error:
        return None
    counts = {}
    for created, data in rows:
        if not started - 5 <= created / 1000.0 <= finished + 60:
            continue
        try:
            model = json.loads(data).get("model")
        except ValueError:
            continue
        if not isinstance(model, dict) or not model.get("id"):
            continue
        key = _model_key(model)
        counts[key] = counts.get(key, 0) + 1
    return counts


def session_model_index(db_path=None):
    """{session id: "<provider>/<id>#<variant>"} for every session opencode recorded; {} when unreadable."""
    path = Path(db_path) if db_path else OPENCODE_DB
    if not path.exists():
        return {}
    try:
        connection = _connect(path)
        rows = connection.execute("SELECT id, model FROM session_v2").fetchall()
        connection.close()
    except sqlite3.Error:
        return {}
    index = {}
    for session_id, model in rows:
        try:
            parsed = json.loads(model) if model else None
        except ValueError:
            parsed = None
        if session_id and isinstance(parsed, dict):
            index[session_id] = _model_key(parsed)
    return index


def _kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


def opencode_command():
    """The opencode executable itself, not the npm shim: the shim's cmd.exe layer mangles arguments."""
    shim = shutil.which("opencode.cmd") or shutil.which("opencode")
    if not shim:
        raise OSError("opencode is not on PATH")
    native = Path(shim).parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
    return [str(native)] if os.name == "nt" and native.exists() else [shim]


def run_task(workspace, task_id, role, task_md, write_paths, shell, model=None, session=None,
             timeout_min=None, _attempt=0):
    """Run one agent. Returns a dict with status, text, tokens, steps, session_id, duration_s, error."""
    workspace = Path(workspace)
    agent = "%s-%s" % (role, task_id)
    roles.write_agent(workspace, agent, role, write_paths, shell)
    limit = (timeout_min or config.TIMEOUT_MIN[role]) * 60
    # A private server per task: it scans the workspace for agent files at start-up. The shared
    # background service learns new agent files asynchronously and may not know this one yet.
    cmd = opencode_command() + ["run", "--standalone", "--agent", agent, "--format", "json", "--auto",
                                "--archived", "--title", "tuning-%s" % task_id]
    if session:
        cmd += ["--session", session]
    cmd += ["--model", model or config.MODELS[role]]
    cmd.append("你的任务书在 %s。先完整读它，然后严格按它执行。" % task_md)

    # opencode takes its project directory from $PWD/$OLDPWD, not from the child's working directory.
    env = dict(os.environ, PWD=Path(workspace).as_posix(), OLDPWD=Path(workspace).as_posix())
    started = time.time()
    result = dict(task_id=task_id, role=role, model=model or config.MODELS[role], status="error", text="",
                  tokens=dict(input=0, output=0, reasoning=0, cache_read=0, cache_write=0), steps=0,
                  tool_calls=0, session_id=None, duration_s=0.0, error="", models=None)
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
                                    stdin=subprocess.DEVNULL, env=env)
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
    # What ran, read back from opencode's own record rather than assumed from the role.
    result["models"] = session_models(state["session"], started, time.time())
    if result["status"] != "ok" and not result["error"]:
        result["error"] = " | ".join(err_tail[-5:])[-600:] or "no output"
    if result["status"] == "ok" and result["steps"] == 0:
        result.update(status="error", error="agent produced no steps")
    if "Agent not found" in result["error"] and _attempt < 2:
        # The background service learns a new agent file through a file watcher; give it a moment.
        time.sleep(4)
        return run_task(workspace, task_id, role, task_md, write_paths, shell, model=model, session=session,
                        timeout_min=timeout_min, _attempt=_attempt + 1)
    folder = workspace / "tasks" / task_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "llm.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "output.md").write_text(result["text"] + "\n", encoding="utf-8")
    return result


def tokens_total(tokens):
    return sum(tokens.values())
