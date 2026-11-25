"""Static gates a candidate must pass before any real run (sections 6, 9 and 11).

A candidate is a directory: ``card.json`` (what and why), ``policy.json`` (config
it sets), ``overlay/`` (package files it replaces or adds), ``tests/`` (unittest
files run against the patched harness). ``static_gate`` reads all of it and
reports issues; it never runs anything.
"""
import hashlib
import json
from pathlib import Path

from . import scope
from .config import ACTIONS, FEEDBACKS, SIGNATURE_METRICS, SIGNALS, TIMINGS, WORKLOADS

METRIC_PATHS = {"overload_s": ("overload", "overload_s"), "near_overload_s": ("overload", "near_overload_s"),
                "peak_demand_GBs": ("overload", "peak_demand_GBs"), "link_utilization": ("overload", "link_utilization"),
                "idle_s": ("workers", "idle_s"), "read_wait_s": ("read_wait_s", None),
                "blocked_total_s": ("permits", "blocked_total_s"), "heavy_hints": ("permits", "heavy_hints")}
CARD_KEYS = ("name", "tier", "mechanism", "params", "causal", "signature", "failure", "protection")


class GateResult:
    def __init__(self):
        self.issues = []
        self.warnings = []
        self.allow_lines = []
        self.files = {}
        self.params = {}

    @property
    def passed(self):
        return not self.issues

    def as_dict(self):
        return dict(passed=self.passed, issues=self.issues, warnings=self.warnings,
                    allow_lines=self.allow_lines, files=sorted(self.files), params=sorted(self.params))


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def candidate_files(cand_dir):
    """Every file of a candidate directory, as {relative path: sha256}."""
    out = {}
    for path in sorted(Path(cand_dir).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc") \
                and path.name not in ("helper.log", "manifest.json", "unittest.txt", "gate.json", "smoke.json"):
            out[str(path.relative_to(cand_dir)).replace("\\", "/")] = _digest(path)
    return out


def write_manifest(cand_dir):
    (Path(cand_dir) / "manifest.json").write_text(
        json.dumps(candidate_files(cand_dir), indent=1, sort_keys=True) + "\n", encoding="utf-8")


def manifest_changes(cand_dir):
    """Files that differ from the recorded manifest, and manifest entries that vanished."""
    path = Path(cand_dir) / "manifest.json"
    if not path.exists():
        return []
    recorded = _read_json(path)
    current = candidate_files(cand_dir)
    return sorted(k for k, v in recorded.items() if current.get(k) != v)


def load_policy(cand_dir):
    path = Path(cand_dir) / "policy.json"
    return _read_json(path) if path.exists() else {}


def load_card(cand_dir):
    path = Path(cand_dir) / "card.json"
    if not path.exists():
        return None, ["card.json 不存在"]
    try:
        card = _read_json(path)
    except ValueError as error:
        return None, ["card.json 不是合法 JSON：%s" % error]
    if not isinstance(card, dict):
        return None, ["card.json 必须是一个对象"]
    return card, []


def _get_path(policy, dotted):
    node = policy
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def card_tier(card, max_tier):
    """The tier a candidate declares, clamped to what is open."""
    tier = card.get("tier") if isinstance(card, dict) else 0
    return tier if isinstance(tier, int) and not isinstance(tier, bool) and 0 <= tier <= max_tier else 0


def check_card(card, max_tier):
    """Schema, mechanism tags, parameter ranges and signature promises."""
    result = GateResult()
    add = result.issues
    missing = [key for key in CARD_KEYS if key not in card]
    if missing:
        add.append("card.json 缺少字段：%s" % "、".join(missing))
        return result
    if not isinstance(card["name"], str) or not card["name"].strip():
        add.append("name 不能为空")
    tier = card["tier"]
    if not isinstance(tier, int) or isinstance(tier, bool) or tier < 0:
        add.append("tier 必须是不小于 0 的整数（0 表示只用核心范围）")
    elif tier > max_tier:
        add.append("card.tier=%d，但第二梯队目前只放开到第 %d 档" % (tier, max_tier))
    mechanism = card["mechanism"]
    for axis, vocabulary in (("signal", SIGNALS), ("action", ACTIONS), ("timing", TIMINGS), ("feedback", FEEDBACKS)):
        tags = mechanism.get(axis) if isinstance(mechanism, dict) else None
        if not isinstance(tags, list) or not tags or any(tag not in vocabulary for tag in tags):
            add.append("mechanism.%s 必须是非空列表，取值限于 %s" % (axis, "/".join(vocabulary)))
    for key in ("causal", "failure", "protection"):
        if not isinstance(card[key], str) or len(card[key].strip()) < 20:
            add.append("%s 至少要用 20 个字说清楚" % key)
    params = card["params"]
    if not isinstance(params, dict):
        add.append("params 必须是对象")
    else:
        for name, spec in params.items():
            numbers = all(isinstance(spec.get(k), (int, float)) and not isinstance(spec.get(k), bool) for k in ("value", "min", "max")) \
                if isinstance(spec, dict) else False
            if not (numbers and spec["min"] <= spec["value"] <= spec["max"]):
                add.append("params[%s] 需要 value、min、max 三个数，且 min <= value <= max" % name)
            else:
                result.params[name] = spec
    signature = card["signature"]
    if not isinstance(signature, list) or not signature:
        add.append("signature 必须是非空列表：承诺哪些诊断量会往哪个方向变")
    else:
        for item in signature:
            if not (isinstance(item, dict) and item.get("workload") in list(WORKLOADS) + ["*"]
                    and item.get("metric") in SIGNATURE_METRICS and item.get("direction") in ("up", "down")):
                add.append("signature 的每一项需要 workload（%s 或 *）、metric（%s）、direction（up/down）"
                           % ("/".join(WORKLOADS), "/".join(SIGNATURE_METRICS)))
    return result


def static_gate(cand_dir, base_dir, max_tier):
    """Everything checkable without running the simulation."""
    cand_dir, base_dir = Path(cand_dir), Path(base_dir)
    card, errors = load_card(cand_dir)
    if errors:
        result = GateResult()
        result.issues.extend(errors)
        return result
    result = check_card(card, max_tier)
    add = result.issues
    tier = card_tier(card, max_tier)

    overlay_root = cand_dir / "overlay"
    overlay = {}
    if overlay_root.exists():
        overlay = {str(p.relative_to(overlay_root)).replace("\\", "/"): p
                   for p in overlay_root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    result.files = dict(overlay)
    policy = load_policy(cand_dir)
    if not overlay and not policy:
        add.append("覆盖层和 policy.json 都是空的：这个候选什么都没改")
    for rel in overlay:
        if rel.endswith(".rs"):
            add.append("%s 改到了路由评分的 Rust 侧：运行器目前不会重新编译 Dynamo，这样的改动不会生效，暂不接受" % rel)
    bad = scope.check_overlay_paths(overlay, tier)
    if bad:
        add.append("overlay 里有范围之外的文件：%s" % bad)
    bad = scope.check_policy(policy, tier)
    if bad:
        add.append("policy.json 里有范围之外的键：%s" % bad)

    for rel, path in overlay.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        base_path = base_dir / scope.PACKAGE / rel
        base_text = base_path.read_text(encoding="utf-8", errors="replace") if base_path.exists() else ""
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as error:
                add.append("%s 语法错误：%s" % (rel, error))
            changed = scope.protected_functions_changed(base_text, text, rel)
            if changed:
                add.append("%s 改动了受保护的函数 %s，必须保持与基线一致" % (rel, "、".join(changed)))
        hard, allowed = scope.forbidden_lines(scope.added_source_lines(base_text, text))
        for line in hard[:12]:
            add.append("%s 新增了不允许的内容：%s" % (rel, line.strip()[:140]))
        result.allow_lines.extend("%s: %s" % (rel, line.strip()[:160]) for line in allowed)
        if base_path.exists() and base_text == text:
            result.warnings.append("%s 与基线完全相同" % rel)

    for name, spec in result.params.items():
        value, found = _get_path(policy, name)
        if not found:
            add.append("params[%s] 在 policy.json 里没有对应的键" % name)
        elif not isinstance(value, (int, float)) or isinstance(value, bool) or abs(value - spec["value"]) > 1e-9:
            add.append("params[%s] 的 value 与 policy.json 里的值不一致" % name)

    tests = sorted((cand_dir / "tests").glob("test_*.py")) if (cand_dir / "tests").exists() else []
    if not tests:
        add.append("tests/ 下没有 unittest 文件（test_*.py）")
    if not any("scale" in path.name.lower() for path in tests):
        add.append("缺少尺度不变性测试：tests/ 下要有一个文件名含 scale 的测试（设计第九节 G2）")
    return result


def signature_check(card, pairs):
    """G5: did the promised diagnostics move the promised way?

    ``pairs``: {workload: {cand: digest, ref: digest, cand_summary, ref_summary}}.
    Returns (kept, missed): the promises that were observed, and those that were not.
    """
    kept, missed = [], []
    for item in card.get("signature", []):
        votes = []
        for name in ([item["workload"]] if item["workload"] != "*" else sorted(pairs)):
            pair = pairs.get(name)
            if not pair:
                continue
            metric = item["metric"]
            try:
                if metric in ("mean_ttft_s", "makespan_s"):
                    before, after = pair["ref_summary"][metric], pair["cand_summary"][metric]
                else:
                    section, key = METRIC_PATHS[metric]
                    before = pair["ref"][section] if key is None else pair["ref"][section][key]
                    after = pair["cand"][section] if key is None else pair["cand"][section][key]
            except (KeyError, TypeError):
                votes.append(False)
                continue
            votes.append(after < before * 0.98 if item["direction"] == "down" else after > before * 1.02)
        text = "%s 上 %s 变%s" % (item["workload"], item["metric"], "小" if item["direction"] == "down" else "大")
        if votes and 2 * sum(votes) >= len(votes):
            kept.append(text)
        else:
            missed.append(text)
    return kept, missed
