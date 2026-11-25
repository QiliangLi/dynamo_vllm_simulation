"""Constants of the tuning workflow. Nothing here is learned or tuned at run time.

The design is docs/TFS自动调优工作流设计-20261125.md (v0.2). Section numbers in
the comments refer to it.
"""
from collections import OrderedDict
from itertools import product

WORKLOADS = OrderedDict([
    ("408", dict(bench="synthetic_abchalfb_408_8s_4_25GBps", title="ABC408")),
    ("540", dict(bench="synthetic_abclonghit_540_12s_4_25GBps", title="ABC540")),
    ("mooncake", dict(bench="mooncake_head875_360_12s_4_25GBps", title="Mooncake 87.5%")),
    ("agentx-narrow", dict(bench="agentx_narrow_358_12s_4_25GBps", title="AgentX 窄区间")),
    ("agentx-flex", dict(bench="agentx_flex_358_12s_4_25GBps", title="AgentX 宽区间")),
])

# Section 5. GLM does the work; kimi-k3.1-pro only cross-checks, a few times per generation.
MODELS = dict(
    proposer="kimi/zhipu/glm-5.4", redteam="kimi/zhipu/glm-5.4", judge="kimi/zhipu/glm-5.4",
    prover="kimi/zhipu/glm-5.4", meta="kimi/zhipu/glm-5.4", toolmaker="kimi/zhipu/glm-5.4",
    analyst="kimi/zhipu/glm-5.3", auditor="kimi/zhipu/glm-5.3",
    crossval="kimi/moonshot/kimi-k3.1-pro")
NON_GLM_ROLES = frozenset({"crossval"})
CROSSVAL_PER_GENERATION = 5
TIMEOUT_MIN = dict(analyst=25, proposer=80, redteam=30, judge=20, auditor=20, prover=25, meta=25,
                   crossval=15, revise=40, toolmaker=30)

# Section 10, frozen.
SCREEN_RUNS = 2
CONFIRM_RUNS = 5
MAX_CONFIRM_RUNS = 15
FINAL_RUNS = 10
CONTROL_EVERY = 10
Z95 = 1.645

# Section 8.
EXPLORE_MIN = 0.40              # share of runs that always goes to new mechanisms
STAGNATION_GENERATIONS = 3      # champion unchanged this many generations: next ladder rung
BASELINE_LIGHT_RUNS = 7
BASELINE_TFS_RUNS = 5
PARALLEL_MAX = 3

LADDER = [
    "放开当前机制的全部参数",
    "组合已验证有效的机制",
    "在核心范围内换环节：分类、路由、释放、排队、反馈",
    "第二梯队 1：逐层预取深度",
    "第二梯队 2：等待队列的基础排序",
    "第二梯队 3：路由评分的 Rust 侧",
    "引入外部思路：检索文献",
    "复活曾被淘汰、失败原因可修的候选",
    "提高测量精度",
]
# Rung index at which each tier-2 scope tier opens (0-based).
TIER2_RUNG = {1: 3, 2: 4, 3: 5}

# Section 8, mechanism map.
SIGNALS = ("count", "bytes", "demand_sum", "measured_rate", "observed_stretch", "wait_time",
           "arrival_rate", "queue_length")
ACTIONS = ("admit", "hold", "release", "order", "route")
TIMINGS = ("arrival", "schedule", "layer", "finish")
FEEDBACKS = ("open_loop", "closed_loop")
MECHANISM_CELLS = ["/".join(cell) for cell in product(SIGNALS, ACTIONS, TIMINGS, FEEDBACKS)]

# Metrics a candidate may promise to move (G5). ``down`` means smaller is better unless stated.
SIGNATURE_METRICS = ("overload_s", "near_overload_s", "peak_demand_GBs", "idle_s", "read_wait_s",
                     "mean_ttft_s", "makespan_s", "blocked_total_s", "heavy_hints", "link_utilization")
DIGEST_METRIC_PATHS = {
    "overload_s": ("overload", "overload_s"), "near_overload_s": ("overload", "near_overload_s"),
    "peak_demand_GBs": ("overload", "peak_demand_GBs"), "link_utilization": ("overload", "link_utilization"),
    "idle_s": ("workers", "idle_s"), "read_wait_s": ("read_wait_s", None),
    "blocked_total_s": ("permits", "blocked_total_s"), "heavy_hints": ("permits", "heavy_hints"),
}
SMOKE_WORKLOAD = "408"

# The stress suite (G4). Built once in stage 0 from the frozen inputs; never enters benchmark/.
STRESS_SPECS = OrderedDict([
    ("bw10_540", dict(kind="bandwidth", source="540", factor=0.4)),
    ("bw50_540", dict(kind="bandwidth", source="540", factor=2.0)),
    ("bw10_agentx", dict(kind="bandwidth", source="agentx-narrow", factor=0.4)),
    ("arr_fast_540", dict(kind="arrival", source="540", factor=0.5)),
    ("arr_slow_540", dict(kind="arrival", source="540", factor=2.0)),
    ("arr_fast_mooncake", dict(kind="arrival", source="mooncake", factor=0.5)),
    ("heavy_only", dict(kind="mix", rule="heavy")),
    ("light_only", dict(kind="mix", rule="light")),
    ("zero_read", dict(kind="mix", rule="zero")),
    ("bimodal", dict(kind="mix", rule="bimodal")),
    ("workers2_408", dict(kind="workers", source="408", workers=2)),
    ("workers8_540", dict(kind="workers", source="540", workers=8)),
])
MIX_SOURCES = ("408", "540", "mooncake")
MIX_COUNT = 300
