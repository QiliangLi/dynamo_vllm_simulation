"""What a tuning candidate may change. Shared by the Windows orchestrator and the WSL runner.

Tier 1 is open from the start. Tier 2 opens in order while the search stalls
(docs/TFS自动调优工作流设计-20261125.md, section 2). Paths are relative to the
package root ``dynamo_vllm_cpu_sim/``. Everything not listed here is frozen for
candidates: storage and compute models, the client, the checkers, the runner.
"""

PACKAGE = "dynamo_vllm_cpu_sim"

# Functions whose body a candidate must leave byte-for-byte identical to the base.
# scheduler_admission patches the pinned vLLM scheduler through them; editing the
# anchor would let a candidate bypass the version guard.
PROTECTED_FUNCTIONS = {
    "sim/stack/scheduler_admission.py": ("patch_schedule", "install_scheduler_adapter"),
}

TIER1_FILES = frozenset({
    # profiles and classification
    "sim/admission.py",
    "sim/request_profile.py",
    "sim/reference_time.py",
    "sim/stack/storage_hint.py",
    "sim/stack/tier_profile.py",
    # ledger and admission service
    "sim/stack/read_admission.py",
    "sim/stack/admission_service.py",
    # scheduler-side gate
    "sim/stack/scheduler_admission.py",
    "sim/stack/scheduler_compat.py",
    # release and feedback path
    "sim/stack/feedback.py",
    "sim/stack/admission_outbox.py",
    "sim/stack/load_observation.py",
    "sim/stack/best_effort.py",
    "sim/stack/admission_rpc.py",
    "sim/stack/admission_rpc_status.py",
    "sim/stack/dynamo_admission_rpc.py",
    # ingress-side triage
    "sim/stack/ingress_control.py",
    "sim/stack/server_ingress_control.py",
    # routing score, Python side
    "sim/stack/routing_accounting.py",
    "sim/stack/async_score.py",
})
TIER1_NEW_PREFIXES = ("sim/stack/policy/",)
TIER1_POLICY_KEYS = frozenset({"admission", "routing_accounting", "request_profile"})

TIER2 = {
    1: dict(
        name="逐层预取深度",
        files=frozenset({"sim/stack/layer_storage.py", "sim/stack/layerwise.py"}),
        policy_keys=frozenset(),
    ),
    2: dict(
        name="等待队列的基础排序",
        files=frozenset({"sim/priority.py", "sim/stack/edf_priority.py", "sim/stack/budget_priority.py"}),
        policy_keys=frozenset({"request_ordering", "policy"}),
    ),
    3: dict(
        name="路由评分的 Rust 侧",
        files=frozenset({
            "patched/weighted_routing/pending_score.rs",
            "patched/weighted_routing/apply_patch.py",
        }),
        policy_keys=frozenset(),
    ),
}
TIER2_ORDER = (1, 2, 3)

# Config keys a candidate may set through policy.json, by tier.
STORAGE_KEYS_FROZEN = ("storage", "compute", "workers", "block_size", "num_blocks", "max_model_len",
                       "max_num_seqs", "max_num_batched_tokens", "kv_transfer_mode", "wall_time_scale",
                       "ttft_slo_s", "server_dynamo", "frontend_tokenizer", "enable_prefix_caching",
                       "router_mode", "kv_connector_implementation")


def allowed_files(max_tier):
    """Files a candidate may replace or add when tiers up to ``max_tier`` are open.

    ``max_tier`` 0 means tier 1 only.
    """
    files = set(TIER1_FILES)
    for tier in TIER2_ORDER:
        if tier <= max_tier:
            files |= TIER2[tier]["files"]
    return files


def allowed_policy_keys(max_tier):
    keys = set(TIER1_POLICY_KEYS)
    for tier in TIER2_ORDER:
        if tier <= max_tier:
            keys |= TIER2[tier]["policy_keys"]
    return keys


def check_overlay_paths(paths, max_tier):
    """Return the list of paths outside the open scope. Empty means all clear."""
    files = allowed_files(max_tier)
    bad = []
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        if ".." in parts or path.startswith("/"):
            bad.append(path)
        elif path in files:
            continue
        elif path.startswith(TIER1_NEW_PREFIXES) and parts[-1].endswith(".py"):
            continue
        else:
            bad.append(path)
    return bad


def check_policy(policy, max_tier):
    """Return the list of policy keys outside the open scope."""
    keys = allowed_policy_keys(max_tier)
    return sorted(k for k in policy if k not in keys)


# Tokens a candidate's added lines must not contain. They would let a policy read
# the workload's identity, touch files, or start processes. A line ending in
# ``# tuning-allow: <reason>`` is exempt and goes to the red team for review.
FORBIDDEN_TOKENS = (
    "class_id", "subprocess", "os.system", "shutil", "ctypes", "__import__", "eval(", "exec(",
    "os.environ", "sys.argv", "open(", "read_text(", "json.load", "Path(",
    "trace.jsonl", "requests.csv", "manifest", "benchmark",
    "abchalfb", "abclonghit", "head875", "agentx", "mooncake", "synthetic_",
)
ALLOW_MARKER = "tuning-allow:"


def forbidden_lines(added_lines):
    """Return ``(hard, allowed)``: offending lines, and exempted ones carrying an allow marker."""
    hard, allowed = [], []
    for text in added_lines:
        if ALLOW_MARKER in text:
            allowed.append(text)
        elif any(token in text for token in FORBIDDEN_TOKENS):
            hard.append(text)
    return hard, allowed


def added_source_lines(base_text, new_text):
    """Lines present in ``new_text`` but not in ``base_text`` (order ignored)."""
    import collections
    old = collections.Counter((base_text or "").splitlines())
    out = []
    for line in (new_text or "").splitlines():
        if old[line]:
            old[line] -= 1
        else:
            out.append(line)
    return out


def protected_functions_changed(base_text, new_text, rel_path):
    """Names of protected functions in ``rel_path`` whose source differs from base."""
    import ast
    names = PROTECTED_FUNCTIONS.get(rel_path)
    if not names:
        return []

    def bodies(text):
        tree = ast.parse(text)
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                found[node.name] = ast.dump(node, include_attributes=False)
        return found

    try:
        before, after = bodies(base_text), bodies(new_text)
    except SyntaxError:
        return list(names)
    return [n for n in names if before.get(n) != after.get(n)]
