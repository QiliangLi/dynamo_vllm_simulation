#!/usr/bin/env python
"""Entry point of the TFS tuning workflow.

  python tune.py init --workspace D:\\Coding\\tfs-tuning [--repo PATH] [--skip-wsl]
  python tune.py daemon            run the orchestrator; returns only after a STOP
  python tune.py status            one screen of text
  python tune.py pause | resume | stop | unfreeze
  python tune.py gate CANDIDATE    (agents) the static gate, offline
  python tune.py test CANDIDATE    (agents) run a candidate's unit tests in WSL
  python tune.py smoke CANDIDATE WORKLOAD   (agents) ask for one replay and wait for it

The default workspace is the directory holding this file.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tuning import config, daemon as core, gates, report, roles, wsl  # noqa: E402
from tuning.store import Store, read_json, write_json  # noqa: E402

BRANCH = "lql_1125_tuning"
BASE_PATHS = ["dynamo_vllm_cpu_sim/sim", "dynamo_vllm_cpu_sim/scripts", "dynamo_vllm_cpu_sim/patched",
              "dynamo_vllm_cpu_sim/minimax_prefill", "scripts/four_worker", "scripts/run_4w25.sh",
              "scripts/tuning/wsl", "scripts/tuning/tuning", "docs/TFS自动调优工作流设计-20261125.md"]
IGNORE = """runs/
candidates/
digest/gen_*/
tasks/
meta/
reviews/
judgements/
audits/
crossval/
generations/
requests/
tools/
control/
repo/
dashboard/
state.json*
ledger.jsonl*
daemon.log
daemon.pid
NEEDS_HUMAN.md
FINAL_REPORT.md
__pycache__/
*.pyc
.opencode/agents/
"""
SUPERVISOR = '''Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base
fast = 0
Do While Not fso.FileExists(base & "\\control\\STOP")
    started = Timer
    code = shell.Run("""%s"" ""tune.py"" daemon", 0, True)
    If code = 0 Then Exit Do
    elapsed = Timer - started
    If elapsed < 0 Then elapsed = elapsed + 86400
    If elapsed < 120 Then fast = fast + 1 Else fast = 0
    If fast >= 5 Then
        Set note = fso.CreateTextFile(base & "\\SUPERVISOR_STOPPED.txt", True)
        note.WriteLine "The daemon exited within two minutes of starting five times in a row."
        note.WriteLine "The supervisor stopped restarting it. See daemon.log and ledger.jsonl."
        note.Close
        Exit Do
    End If
    WScript.Sleep 60000
Loop
'''


def workspace_of(args):
    return Path(getattr(args, "workspace", None) or HERE).resolve()


def write_cmd(path, text):
    """Write a batch file with exactly the line ends it was written with.

    cmd.exe reads CRLF; text mode on Windows would turn each CRLF into CR CR LF.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def write_controls(ws, pythonw):
    """The supervisor loop and the batch files that start it, open the dashboard and control the daemon."""
    supervisor = SUPERVISOR % pythonw
    (ws / "守护.vbs").write_text(supervisor, encoding="utf-8")
    # The same script under an ASCII name: other launchers mangle non-ASCII paths.
    (ws / "supervisor.vbs").write_text(supervisor, encoding="utf-8")
    for name, action in (("暂停.cmd", "PAUSE"), ("停止并收尾.cmd", "STOP")):
        write_cmd(ws / name, '@echo off\r\nchcp 65001 >nul\r\ncd /d "%%~dp0"\r\nif not exist control mkdir control\r\n'
                   'type nul > control\\%s\r\necho 已记录：%s\r\ntimeout /t 3 >nul\r\n' % (action, action))
    write_cmd(ws / "继续.cmd", '@echo off\r\nchcp 65001 >nul\r\ncd /d "%%~dp0"\r\n'
               'if exist control\\PAUSE del control\\PAUSE\r\n'
               'if exist NEEDS_HUMAN.md del NEEDS_HUMAN.md\r\n'
               'if exist control\\STOP del control\\STOP\r\n'
               'echo 已继续。守护进程每 5 秒检查一次。\r\ntimeout /t 3 >nul\r\n')
    write_cmd(ws / "启动看板.cmd", '@echo off\r\ncd /d "%%~dp0"\r\nstart "" "dashboard\\index.html"\r\n')


# ---------------------------------------------------------------- init

def cmd_init(args):
    ws, repo = workspace_of(args), Path(args.repo).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("runs", "control", "dashboard", "requests", "digest", "meta", "tasks"):
        (ws / name).mkdir(exist_ok=True)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()

    # 1. the orchestrator's own code, so the daemon never runs from the user's checkout
    (ws / "tuning").mkdir(exist_ok=True)
    for path in (HERE / "tuning").glob("*.py"):
        shutil.copy2(path, ws / "tuning" / path.name)
    shutil.copy2(HERE / "tune.py", ws / "tune.py")

    # 2. what the agents see
    (ws / "AGENTS.md").write_text(roles.WORKSPACE_RULES, encoding="utf-8")
    (ws / ".opencode" / "agents").mkdir(parents=True, exist_ok=True)
    write_json(ws / ".opencode" / "opencode.json", roles.OPENCODE_CONFIG)
    (ws / ".gitignore").write_text(IGNORE, encoding="utf-8")

    # 3. the frozen package sources, read-only for agents and the diff base of every overlay
    base = ws / "base"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir()
    archive = subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "archive", "HEAD", "--format=tar", "--"]
                             + BASE_PATHS, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(base, filter="data")
    report.write_code_map(ws)

    # 4. state
    store = Store(ws)
    if not (ws / "state.json").exists():
        write_json(ws / "state.json", core.initial_state(commit))
        store.append(dict(kind="init", commit=commit, branch=branch))
    else:
        state = store.load()
        state["base_commit"] = commit
        write_json(ws / "state.json", state)
        store.append(dict(kind="reinit", commit=commit, branch=branch))

    # 5. the workspace is a git repository holding exactly the protected files. The commit
    #    comes last, after every file is written: the daemon's guard reverts any uncommitted
    #    change to a protected file after each model task, which would undo a fresh deploy.
    if not (ws / ".git").exists():
        subprocess.run(["git", "init", "-q", str(ws)], check=True)
    for key, value in (("user.name", "tuning-workflow"), ("user.email", "tuning@localhost")):
        subprocess.run(["git", "-C", str(ws), "config", key, value], check=True)

    # 6. a worktree of the tuning branch, so commits never touch the user's checkout
    if not (ws / "repo").exists():
        result = subprocess.run(["git", "-C", str(repo), "worktree", "add", str(ws / "repo"), BRANCH],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            print("警告：无法建立仓库工作树（%s）。冠军将只保存在工作区，不会提交。" % result.stderr.strip()[-200:])

    # 7. controls
    write_controls(ws, Path(sys.executable).with_name("pythonw.exe"))

    # 8. commit everything that was written, so the tree the guard defends is the deployed one
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "workspace baseline %s" % commit[:10],
                    "--allow-empty"], check=True)

    if not args.skip_wsl:
        wsl_setup(repo)
    print(json.dumps(dict(workspace=str(ws), commit=commit, branch=branch), ensure_ascii=False))
    return 0


def wsl_setup(repo):
    """Import the branch into WSL as the frozen base and hash its harness."""
    print("导入仓库到 WSL（约 820 MB）…")
    rc, out, err, reason = wsl.call("rm -rf %(root)s/repo.new && mkdir -p %(root)s/repo.new %(root)s/runs %(root)s/tmp\n"
                                    % dict(root=wsl.WSL_ROOT), timeout=300)
    if rc != 0:
        raise SystemExit("WSL 不可用：%s %s" % (reason, err[-200:]))
    archive = subprocess.Popen(["git", "-C", str(repo), "-c", "core.autocrlf=false", "archive", "--format=tar", BRANCH],
                               stdout=subprocess.PIPE)
    env = dict(os.environ, WSL_UTF8="1")
    extract = subprocess.run(["wsl.exe", "-d", wsl.DISTRO, "--", "tar", "-xf", "-", "-C", wsl.WSL_ROOT + "/repo.new"],
                             stdin=archive.stdout, env=env)
    archive.stdout.close()
    archive.wait()
    if extract.returncode:
        raise SystemExit("导入失败")
    script = """
set -e
rm -rf %(root)s/repo && mv %(root)s/repo.new %(root)s/repo
cd %(root)s/repo
P=dynamo_vllm_cpu_sim
[ -e $P/.venv ] || ln -s /root/.local/share/dynamo-vllm-simulation/139b58d6a3fb/.venv $P/.venv
[ -e $P/upstream ] || ln -s "$(readlink -f /root/dv-repo-lql_1125_opt/$P/upstream 2>/dev/null || echo /nonexistent)" $P/upstream
test -x $P/.venv/bin/python && echo venv_ok
test -d $P/upstream/vllm && echo upstream_ok || echo upstream_missing
$P/.venv/bin/python scripts/tuning/wsl/run_tuning.py --base-manifest %(root)s/base_manifest.json
echo WSL_SETUP_OK
""" % dict(root=wsl.WSL_ROOT)
    rc, out, err, reason = wsl.call(script, timeout=600)
    print(out.strip()[-600:], err.strip()[-300:])
    if "WSL_SETUP_OK" not in out:
        raise SystemExit("WSL 基线搭建失败（%s）" % reason)


# ---------------------------------------------------------------- agent helpers

def _card_tier(ws, cand_dir):
    card, _ = gates.load_card(cand_dir)
    state = read_json(ws / "state.json") if (ws / "state.json").exists() else {}
    return gates.card_tier(card or {}, core.open_tier(state))


def cmd_test(args):
    ws = workspace_of(args)
    cand_id = args.candidate
    if not cand_id.isalnum():
        print("候选编号只能是字母和数字")
        return 2
    cand_dir = ws / "candidates" / cand_id
    if not cand_dir.exists():
        print("没有这个候选目录：candidates/%s" % cand_id)
        return 2
    status, detail = wsl.candidate_tests(cand_dir, _card_tier(ws, cand_dir))
    print("单测结果：%s" % {"pass": "通过", "fail": "未通过", "missing": "没有测试", "error": "无法运行"}[status])
    print(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
    with open(cand_dir / "helper.log", "a", encoding="utf-8") as stream:
        stream.write("%s test %s %s\n" % (time.strftime("%H:%M:%S"), cand_id, status))
    return 0 if status == "pass" else 1


def cmd_gate(args):
    """The static gate, offline: scope, protected functions, forbidden content, card, parameters, tests."""
    ws = workspace_of(args)
    cand_id = args.candidate
    if not cand_id.isalnum():
        print("候选编号只能是字母和数字")
        return 2
    cand_dir = ws / "candidates" / cand_id
    if not cand_dir.exists():
        print("没有这个候选目录：candidates/%s" % cand_id)
        return 2
    state = read_json(ws / "state.json") if (ws / "state.json").exists() else {}
    result = gates.static_gate(cand_dir, ws / "base", core.open_tier(state))
    print("静态门禁：%s" % ("通过" if result.passed else "未通过"))
    for issue in result.issues:
        print("  问题：" + issue)
    for warning in result.warnings:
        print("  提醒：" + warning)
    if result.allow_lines:
        print("  带许可标记、将由红队复核的行：")
        for line in result.allow_lines:
            print("    " + line)
    with open(cand_dir / "helper.log", "a", encoding="utf-8") as stream:
        stream.write("%s gate %s %s\n" % (time.strftime("%H:%M:%S"), cand_id, "pass" if result.passed else "fail"))
    return 0 if result.passed else 1


def cmd_smoke(args):
    ws = workspace_of(args)
    cand_id, workload = args.candidate, args.workload
    if not cand_id.isalnum() or workload not in config.WORKLOADS:
        print("用法：python tune.py smoke <候选编号> <408|540|mooncake|agentx-narrow|agentx-flex>")
        return 2
    cand_dir = ws / "candidates" / cand_id
    if not cand_dir.exists():
        print("没有这个候选目录：candidates/%s" % cand_id)
        return 2
    requests = ws / "requests"
    if len(list(requests.glob("smoke_%s_*.started.json" % cand_id))) >= 2:
        print("这个候选已经用过两次冒烟回放，不能再申请。")
        return 1
    stem = "smoke_%s_%d" % (cand_id, int(time.time()))
    policy = read_json(cand_dir / "policy.json") if (cand_dir / "policy.json").exists() else None
    write_json(requests / (stem + ".json"), dict(cand=cand_id, workload=workload, policy=policy, at=time.time()))
    print("已申请一次 %s 回放，等待编排器安排（守护进程必须在运行）…" % workload)
    deadline = time.time() + 1500
    run_id = None
    while time.time() < deadline:
        started = requests / (stem + ".started.json")
        if started.exists():
            run_id = read_json(started)["run"]
            break
        time.sleep(5)
    if not run_id:
        print("15 分钟内没有被受理。确认守护进程在运行，然后重试。")
        return 1
    print("已排队：%s" % run_id)
    while time.time() < deadline:
        path = ws / "runs" / run_id / "result.json"
        if path.exists():
            result = read_json(path)
            if result["status"] == "done":
                write_json(cand_dir / "smoke_agent.json", dict(run=run_id, workload=workload, summary=result["summary"], digest=result["digest"]))
                print("完成。平均 TTFT %.3f s，完成时间 %.3f s。明细在 candidates/%s/smoke_agent.json。" % (
                    result["summary"]["mean_ttft_s"], result["summary"]["makespan_s"], cand_id))
                return 0
            print("回放失败：%s\n%s" % (result.get("error"), (result.get("log_tail") or "")[-1500:]))
            return 1
        time.sleep(5)
    print("等待超时。")
    return 1


# ---------------------------------------------------------------- control

def cmd_daemon(args):
    ws = workspace_of(args)
    log = open(ws / "daemon.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    core.Daemon(ws).loop()
    return 0


def cmd_status(args):
    ws = workspace_of(args)
    state = read_json(ws / "state.json") if (ws / "state.json").exists() else {}
    d = core.Daemon(ws)
    d.state = state
    champion = state.get("candidates", {}).get(state.get("champion")) if state.get("champion") else None
    print("阶段：%s%s   第 %d 代（%s）   并行度 %d" % (
        state.get("phase"), ("  需要人工处理：" + state["needs_human"]) if state.get("needs_human") else "",
        state.get("gen", {}).get("n", 0), state.get("gen", {}).get("step"), state.get("parallel", 1)))
    print("冠军：%s %s" % (state.get("champion") or "无", (champion or {}).get("card", {}).get("name", "") if champion else ""))
    print("运行：%d 轮（成功 %d，失败 %d）  排队 %d  在跑 %d  WSL：%s" % (
        state.get("runs_total", 0), state.get("runs_ok", 0), state.get("runs_failed", 0), len(state.get("queue", [])),
        len(state.get("active", {})), "正常" if d.health()["ok"] else d.health()["reason"]))
    for row in (state.get("alerts") or [])[-5:]:
        print("  [%s] %s %s" % (row["level"], row["at"], row["text"]))
    return 0


def cmd_control(args):
    ws = workspace_of(args)
    control = ws / "control"
    control.mkdir(exist_ok=True)
    if args.action == "pause":
        (control / "PAUSE").write_text("")
    elif args.action == "stop":
        (control / "STOP").write_text("")
    elif args.action == "resume":
        for name in ("PAUSE", "STOP"):
            (control / name).unlink(missing_ok=True)
        (ws / "NEEDS_HUMAN.md").unlink(missing_ok=True)
    elif args.action == "unfreeze":
        store = Store(ws)
        state = store.load()
        before = state.get("frozen")
        state["frozen"] = core.frozen_hashes()
        write_json(ws / "state.json", state)
        store.append(dict(kind="unfreeze", before=before, after=state["frozen"]))
        print("已登记冻结区的新哈希。")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--repo", default=str(HERE.parent.parent))
    init.add_argument("--skip-wsl", action="store_true")
    sub.add_parser("daemon")
    sub.add_parser("status")
    test = sub.add_parser("test")
    test.add_argument("candidate")
    gate = sub.add_parser("gate")
    gate.add_argument("candidate")
    smoke = sub.add_parser("smoke")
    smoke.add_argument("candidate")
    smoke.add_argument("workload")
    control = sub.add_parser("control")
    control.add_argument("action", choices=["pause", "resume", "stop", "unfreeze"])
    args = parser.parse_args()
    if args.command == "control":
        return cmd_control(args)
    return {"init": cmd_init, "daemon": cmd_daemon, "status": cmd_status, "test": cmd_test, "gate": cmd_gate, "smoke": cmd_smoke}[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
