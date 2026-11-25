"""Undo anything an agent wrote outside its task (section 11).

The workspace is a git repository whose tracked files are exactly the protected
ones: ``base/``, the agent configuration, the orchestrator code, the code map.
Everything the workflow produces is git-ignored. So after every LLM task, any
modified, deleted or stray file that ``git status`` reports is a violation, and
restoring it is ``git checkout`` plus ``git clean``.
"""
import subprocess
from pathlib import Path

from . import gates


def _git(workspace, *args):
    return subprocess.run(["git", "-C", str(workspace)] + list(args), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def changes(workspace):
    """[(status code, path)] of everything git sees as changed or stray."""
    out = _git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = []
    for item in out.split("\0"):
        if len(item) > 3:
            entries.append((item[:2], item[3:].replace("\\", "/")))
    return entries


def restore(workspace):
    """Revert every change to tracked files and delete stray files. Returns what it undid."""
    entries = changes(workspace)
    if not entries:
        return []
    tracked = [path for code, path in entries if code != "??"]
    stray = [path for code, path in entries if code == "??"]
    if tracked:
        _git(workspace, "checkout", "--", *tracked)
    if stray:
        _git(workspace, "clean", "-f", "--", *stray)
    return entries


def tampered_candidates(state, workspace, own):
    """Candidates whose files no longer match the manifest written when they were finalized."""
    out = []
    for cand_id, cand in state["candidates"].items():
        if cand_id == own or cand.get("status") in ("rejected", "failed", "retired"):
            continue
        folder = Path(workspace) / "candidates" / cand_id
        if not (folder / "manifest.json").exists():
            continue
        if gates.manifest_changes(folder):
            out.append(cand_id)
    return out
