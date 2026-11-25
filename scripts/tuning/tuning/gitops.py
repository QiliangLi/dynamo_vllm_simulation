"""Recording champions in the repository.

The daemon owns a separate git worktree of the tuning branch (``<workspace>/repo``),
so the user's own checkout is never touched, switched or dirtied. Every champion
is copied under ``scripts/tuning/champions/<id>/`` with its evidence, committed
and tagged; a failed push is logged and retried at the next promotion.
"""
import difflib
import json
import shutil
import subprocess
import time
from pathlib import Path

BRANCH = "lql_1125_tuning"
ROOT = "scripts/tuning"


def _git(repo, *args, timeout=300):
    return subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def worktree(daemon):
    return daemon.ws / "repo"


def _ready(repo):
    return (repo / ".git").exists() and _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == BRANCH


def _sync(repo):
    """Commit what is staged, then push the branch and its tuning tags. Returns a problem text or ''."""
    _git(repo, "push", "origin", BRANCH, "--follow-tags")
    if _git(repo, "status", "--porcelain").stdout.strip():
        return "工作树不干净，暂不提交"
    return ""


def record_champion(daemon, cand_id):
    repo = worktree(daemon)
    if not _ready(repo):
        raise RuntimeError("仓库工作树不可用（%s）" % repo)
    cand = daemon.state["candidates"][cand_id]
    source = daemon.ws / "candidates" / cand_id
    dest = repo / ROOT / "champions" / cand_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("card.json", "CARD.md", "policy.json"):
        if (source / name).exists():
            shutil.copy2(source / name, dest / name)
    for name in ("overlay", "tests"):
        if (source / name).exists():
            shutil.copytree(source / name, dest / name, ignore=shutil.ignore_patterns("__pycache__"))
    evidence = dict(id=cand_id, generation=cand["gen"], kind=cand["kind"], base_commit=daemon.state["base_commit"],
                    score=cand.get("score"), results=cand.get("results"), stress=cand.get("stress"),
                    signature=cand.get("signature"), leave_one_out=cand.get("leave_one_out"),
                    judge=cand.get("judge"), audit=cand.get("audit"), tier=cand.get("tier", 0), recorded=time.time())
    (dest / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _git(repo, "add", "--", str(dest.relative_to(repo)).replace("\\", "/"))
    _git(repo, "commit", "-q", "-m", "TFS自动调优冠军 %s：第 %d 代，排序分 %s（基线提交 %s）"
         % (cand_id, cand["gen"], "%.2f%%" % (100 * (cand.get("score") or 0)), daemon.state["base_commit"][:10]))
    _git(repo, "tag", "-f", "tfs-tuning/%s" % cand_id)
    problem = _sync(repo)
    daemon.state.setdefault("unpushed", []).append(cand_id) if problem else None
    if problem:
        raise RuntimeError(problem)


def champion_patch(daemon, cand_id):
    """A ``patch -p1`` diff from the frozen base to the champion's overlay, plus its policy."""
    cand = daemon.state["candidates"][cand_id]
    source = daemon.ws / "candidates" / cand_id
    pieces = []
    overlay = source / "overlay"
    for path in sorted(overlay.rglob("*")) if overlay.exists() else []:
        if not path.is_file():
            continue
        rel = str(path.relative_to(overlay)).replace("\\", "/")
        base = daemon.base_dir / "dynamo_vllm_cpu_sim" / rel
        before = base.read_text(encoding="utf-8").splitlines(keepends=True) if base.exists() else []
        after = path.read_text(encoding="utf-8").splitlines(keepends=True)
        pieces.extend(difflib.unified_diff(before, after, fromfile="a/" + rel if base.exists() else "/dev/null", tofile="b/" + rel))
    return "".join(pieces), json.dumps(json.loads((source / "policy.json").read_text(encoding="utf-8")) if (source / "policy.json").exists() else {},
                                       ensure_ascii=False, indent=2) + "\n"


RUNNER = r"""#!/usr/bin/env bash
# Replay the TFS champion produced by the automatic tuning workflow.
# Usage: bash scripts/tuning/champion/run_4w25_tuned.sh 408|540|mooncake|agentx-narrow|agentx-flex NEW_OUTPUT_DIR
# Same environment variables as scripts/run_4w25.sh (DV_PYTHON, DYNAMO_PYTHON_DIR). The harness is built
# by scripts/tuning/wsl/run_tuning.py, so the champion's overlay and policy are applied exactly as tuned.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
here=$(cd "$(dirname "$0")" && pwd)
if [[ $# != 2 ]]; then
    echo "Usage: bash scripts/tuning/champion/run_4w25_tuned.sh 408|540|mooncake|agentx-narrow|agentx-flex NEW_OUTPUT_DIR" >&2
    exit 2
fi
python=${DV_PYTHON:-$repo/dynamo_vllm_cpu_sim/.venv/bin/python}
server=${DYNAMO_PYTHON_DIR:-$HOME/.cache/dynamo-server-sim/build-1.2/python}
export PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS=''
mkdir -p "$(dirname "$2")"
mkdir "$2"
out=$(cd "$2" && pwd)
case "$1" in
    408) bench=synthetic_abchalfb_408_8s_4_25GBps ;;
    540) bench=synthetic_abclonghit_540_12s_4_25GBps ;;
    mooncake) bench=mooncake_head875_360_12s_4_25GBps ;;
    agentx-narrow) bench=agentx_narrow_358_12s_4_25GBps ;;
    agentx-flex) bench=agentx_flex_358_12s_4_25GBps ;;
    *) echo "Unknown workload: $1" >&2; exit 2 ;;
esac
"$python" "$repo/scripts/tuning/wsl/run_tuning.py" --base-manifest "$out/base_manifest.json" > /dev/null
"$python" - "$out" "$repo" "$here" "$bench" "$server" <<'SPEC'
import json, os, sys
out, repo, here, bench, server = sys.argv[1:6]
evidence = json.load(open(here + "/evidence.json"))
spec = dict(run_id="champion", bench=bench, bench_dir=repo + "/benchmark/" + bench, arm="candidate",
            out=out + "/run", workers=4, port_offset=0, max_tier=evidence.get("tier", 0), timeout_s=1500,
            base_manifest=out + "/base_manifest.json", server=server, export_dir=None,
            overlay=here + "/overlay" if os.path.isdir(here + "/overlay") else None,
            policy=json.load(open(here + "/policy.json")) if os.path.exists(here + "/policy.json") else None)
json.dump(spec, open(out + "/spec.json", "w"))
SPEC
"$python" "$repo/scripts/tuning/wsl/run_tuning.py" "$out/spec.json"
echo "Result: $out/run/result.json"
"""


def merge_champion(daemon, cand_id):
    """Write the champion as a runnable entry in the worktree, commit and push."""
    repo = worktree(daemon)
    if not _ready(repo):
        raise RuntimeError("仓库工作树不可用（%s）" % repo)
    record_champion(daemon, cand_id) if not (repo / ROOT / "champions" / cand_id).exists() else None
    patch, policy = champion_patch(daemon, cand_id)
    dest = repo / ROOT / "champion"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "champion.patch").write_text(patch, encoding="utf-8", newline="\n")
    (dest / "policy.json").write_text(policy, encoding="utf-8", newline="\n")
    (dest / "run_4w25_tuned.sh").write_text(RUNNER, encoding="utf-8", newline="\n")
    card = json.loads((daemon.ws / "candidates" / cand_id / "card.json").read_text(encoding="utf-8"))
    (dest / "README.md").write_text(
        "# 自动调优产出的 TFS 冠军\n\n候选 `%s`，第 %d 代。机制：%s。\n\n- `champion.patch`：相对冻结基线的 `patch -p1` 补丁。\n"
        "- `policy.json`：它下发的配置。\n- `run_4w25_tuned.sh <负载> <新目录>`：按 `run_4w25.sh` 的环境回放。\n\n"
        "因果链：%s\n\n证据见 `../champions/%s/evidence.json`。\n" % (cand_id, daemon.state["candidates"][cand_id]["gen"], card.get("name"), card.get("causal"), cand_id),
        encoding="utf-8")
    _git(repo, "add", "--", ROOT + "/champion")
    _git(repo, "commit", "-q", "-m", "TFS自动调优收尾：冠军 %s 合入为 run_4w25_tuned.sh 入口" % cand_id)
    _git(repo, "tag", "-f", "tfs-tuning/final")
    problem = _sync(repo)
    if problem:
        raise RuntimeError(problem)
