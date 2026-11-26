"""What the agents read (markdown digests) and what the user watches (the dashboard).

Everything here is derived from the state document and the run results; nothing
is decided here. ``code_map`` is written once at deploy: where TFS logic lives,
what the frozen checkers demand of any candidate, and which signals the ledger
can see when it decides.
"""
import html
import json
import statistics
import time
from pathlib import Path

from . import config, evaluator, roles
from .store import read_json

W = list(config.WORKLOADS)


def pct(value, digits=1):
    return "-" if value is None else "%+.*f%%" % (digits, 100 * value)


def num(value, digits=3):
    return "-" if value is None else ("%.*f" % (digits, value))


def _cell(value):
    """A table cell. A candidate's name may contain a pipe or a line break; either would end the cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers, rows):
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return "\n".join(out)


# ---------------------------------------------------------------- code map

CODE_MAP = """# TFS 代码地图与硬约束

本文件描述仿真栈里读调度逻辑的位置、冻结校验对候选的硬性要求、以及账本在决策时刻能看到什么。提案前必读。
所有路径相对 `base/dynamo_vllm_cpu_sim/`。

## 1. 一条请求怎么流过 TFS

```
客户端按计划到达发 HTTP → Dynamo frontend（kv router，原生选实例，α 影响等待重读的计分）
  → 入口服务 sim.stack.admission_service：按 tier 查询结果建画像（压力 = 读取字节 / 独占计算时间）
      → 账本 on_arrivals：按 P75 求 θ，cap = max(1, floor(B/θ))，给请求冻结一个「是否重读」的提示
  → 提示随请求下发到 vLLM worker
  → worker 的 ReadGate：重读请求在外部 KV 查询之前向账本申请许可；被拒（原因 full）则本轮跳过
  → 分配 slot，逐层读取与计算
  → HTTP 首 token 反馈到账本 → 释放许可
```

## 2. 文件与职责（核心范围内、可以替换的文件）

| 文件 | 职责 | 关键位置 |
|---|---|---|
| `sim/admission.py` | `AdmissionController`：θ 与 cap 的计算、重读判定、许可的申请与释放（网关侧的老逻辑） | `on_arrivals`（θ、cap）、`is_heavy`、`held`、`on_event` |
| `sim/stack/read_admission.py` | `FrozenClassification`：到达时冻结分类并下发提示；`acquire` 是许可判定的核心 | `on_arrivals`（提示）、`acquire`（判满）、`rollback`、`on_event` |
| `sim/stack/admission_service.py` | 中央账本服务：HTTP 入口、画像建立、控制消息（acquire / rollback / routing_ready）、状态快照 | `arrive`、`control`、`create_app`、`snapshot` |
| `sim/stack/scheduler_admission.py` | worker 侧的 `ReadGate`：何时申请、被拒后如何跳过、空闲放行 | `ReadGate.allow`、`begin_step`。`patch_schedule` 与 `install_scheduler_adapter` 受保护，不能改 |
| `sim/stack/feedback.py`、`admission_outbox.py`、`best_effort.py`、`admission_rpc*.py`、`dynamo_admission_rpc.py` | worker 与账本之间的控制通路 | 客户端、重试、状态收集 |
| `sim/stack/load_observation.py` | worker 本地的加载事实日志（逐层提交、完成），目前**不**发给账本 | `LoadObserver` |
| `sim/stack/routing_accounting.py`、`async_score.py` | 路由计分账本的 Python 侧客户端，α = `waiting_factor` | `ScoreClient` |
| `sim/request_profile.py`、`sim/reference_time.py` | 画像：需求怎么估。压力 = 读取字节 / 独占计算时间 | `build_profile`、`estimate`、`pressure` |
| `sim/stack/storage_hint.py`、`tier_profile.py`、`ingress_control.py`、`server_ingress_control.py` | 提示的校验、tier 画像、入口侧分诊 | |

新增模块放在 `sim/stack/policy/` 下。配置通过 `policy.json` 的 `admission`、`routing_accounting`、`request_profile` 三个节下发。

第二梯队（停滞时才放开）：`sim/stack/layer_storage.py`、`layerwise.py`（预取深度）；`sim/priority.py`、`sim/stack/edf_priority.py`、`budget_priority.py` 与配置键 `request_ordering`、`policy`（排队）；`patched/weighted_routing/*.rs`（路由评分 Rust 侧）。

## 3. 冻结校验的硬约束

每一轮回放结束后，仓库自带的校验脚本（`scripts/run_tfs_stack.py` 的 `verify`、`scripts/check_read_admission.py`）都会审计账本事件和 worker 日志。**不满足就整轮失败**。候选必须遵守：

1. 拒绝许可时，返回的 `reason` 只能是 `"full"`，且 `ok` 为 True。不能发明新的拒绝原因。
2. 只有提示里 `is_heavy_read` 为 True 的请求可以申请许可。轻读请求永远不能持有许可。想让所有带读取的请求都受管，就把它们全部标成重读。
3. 每个有加载的重读请求，恰好有一个「已释放」的许可。顺序必须是：提示下发 ≤ 许可授予 ≤ 加载提交 ≤ 加载完成 ≤ 首 token ≤ 许可释放。释放原因必须是 `first_token`。**许可不能在首 token 之前释放。**
4. 账本事件里的 `held_before`、`held_after` 会被独立重算：授予时加一，释放时减一，必须与事件里的一致。
5. 普通授予必须满足 `cap is None or held_after <= cap`。要超发，只能在事件里把 `cap` 记为 None。
6. 每条请求都要收到存储提示；worker 日志里的提示必须与账本里的一致。
7. 所有记录结束时状态为 FINISHED 且不持有许可；所有许可最终是 released 或 rolled_back。
8. 账本状态里 `execution_feedback` 现在是 `disabled`，此时不允许出现带 `engine_id` 的 `feedback_received` 事件。如果要让执行器向账本上报加载开始/结束，必须把这两个地方一起改一致。
9. 记录的时间顺序：`arrival_s <= submitted_s <= first_token_s <= finish_s`，`load_submit_s <= load_done_s <= first_token_s`。
10. 执行侧的计算模型由 `check_minimax_steps` 以 1e-12 的误差校验，不能动。请求数、读取字节、模型计算总量会与基线逐值比对。
11. **账本事件总数一轮不能超过 10000 条。** 入口每注册一个请求，就把账本的事件表裁到最近 10000 条（`sim/stack/ingress_control.py` 的 `register` 末尾，`server_ingress_control.py` 同）。校验要把 worker 加载日志里的每个请求关联到它的 `request_correlated` 事件；一旦越线，最早的这些事件被挤掉，整轮以 `KeyError` 失败在 `load_observation.py` 的 `with_local_observations`。这与候选代码的对错无关，只与事件量有关。固定开销：默认策略在 408、mooncake、AgentX 上约 4000–4500 条；**540 上带 TFS 的候选约 8000 条，余量只有约 2000 条**。基线版账本每次拒绝申请都记一条 `read_permit_denied`，而 worker 默认每 2 ms 重试一次（配置键 `admission.retry_interval_wall_s`），所以拒绝频繁的候选在 540 上必然越线：实测一个候选被拒 1354–2108 次时，8 轮里 1 轮失败。做法任选其一或组合：不逐次记录拒绝（按请求聚合，或只记首次）；放慢被拒后的重试；让被拒的请求等到有许可释放再申请。冒烟结果里 `digest.admission_events.total` 是这一轮的账本事件总数（`digest.permits.denied` 是拒绝次数）。**冒烟会在 408 和 540 上各跑一轮，任一轮的事件总数达到 9200 条，候选就会被退回修改，不进入筛选；540 上的目标是不超过 9000 条。**

把这些约束换个角度看：**「正式许可」和「你自己的带宽预算记账」可以分开。** 校验只管正式许可的生命周期。一个候选可以让正式许可照旧在首 token 释放，同时在账本里另记一本「在读需求」的账，在加载完成时就不再计入需求。这样的机制不违反任何一条。

## 4. 账本在决策时刻能看到什么

- 能看到：每条已到达请求的画像（计划到达时刻、估计读取字节、压力、参考计算时间）、自己冻结的提示、HTTP 首 token 与完成反馈、worker 发来的 acquire / rollback 请求及其时刻。
- 看不到（默认）：每次加载何时开始、何时结束，每一层读取的实际耗时。这些只写在 worker 本地的 `worker*.steps.loads.jsonl` 里。想知道在途读取，要么补一条反馈通路（见第 3 节第 8 条），要么用近似：重读请求发出 acquire 的时刻约等于它开始读取；首 token 约等于读取结束。
- worker 侧（`ReadGate`）能看到：本实例的等待队列、运行队列、每条请求的提示、本实例的许可。

## 5. 每一轮的运行配置（五个负载相同，除非注明）

- 入口：`scripts/run_4w25.sh` 的步骤，4 个 worker，每个同时只跑 1 条请求（`max_num_seqs` 1），FCFS，`request_ordering` fcfs。
- 存储：6 个盘各 4.167 GB/s，共享链路 25 GB/s（`shared_link_Bps` 25e9），`bandwidth_policy` max_min_fair，`kv_bytes_per_token` 65536，`read_latency_s` 0.0001。
- 读取方式：`kv_transfer_mode` layer_pipeline，提前一层预取，16 层。
- 准入：`admission.mode` tfs，`quantile` 0.75，`reference_bandwidth_Bps` 25e9，`release_on` first_token，`aging_enabled` false，`placement` scheduler，`classification` arrival，`retry_interval_wall_s` 0.002（墙钟秒），`idle_bypass` false，`profile_source` dynamo_tier，`native_ingress` true。候选只能通过 `policy.json` 改 `admission`、`routing_accounting`、`request_profile` 三个节里的键。
- 路由：`router_mode` kv（Dynamo 原生 kv router 选实例），`routing_accounting.enabled` true，`waiting_factor`（α）0.1。
- `wall_time_scale` 1：模型秒就是墙钟秒。

## 6. 诊断量的定义

逐轮摘要 `digest` 里的量，时间轴都以客户端首次发送为零点：

- `overload.overload_s`：在途请求的峰值需求之和超过链路带宽的时长。一条请求从首层读取发出到末层读取完成都算在途。
- `overload.layer_overload_s`：只在其层读取尚未完成的瞬间计入需求。
- `overload.saturated_s`：实际吞吐达到链路带宽 90% 以上的时长。
- `overload.read_stretch_median / p95`：层读取实际耗时 /（字节 ÷ 链路带宽）。
- `workers.idle_s`：四个实例在运行窗口内没有执行请求的总时间。`starved_idle_s` 是其中「本实例已有请求到达在等」的那部分。
- `read_wait_s`：所有步的（步耗时 − 模型计算时长）之和，主要是等 KV。
- `permits.*`：重读提示数、授予数、被拒次数、被挡住的请求数与总等待时间。
- `reference.*`：无争用参考（每条请求按独占链路速度、同样的实例归属、FCFS）；计算下界 = 计算总量 / 实例数；链路下界 = 读取量 / 带宽。
- `timeline`：0.25 秒一格的在途需求与正在执行的请求数。

## 7. 自测命令

- `python tune.py gate <候选编号>`：离线跑一遍静态门禁（范围、受保护的函数、被禁内容、卡片字段、参数与测试是否齐全），不占用仿真机。
- `python tune.py test <候选编号>`：在 WSL 里按你的覆盖层搭一份仿真环境，运行 `tests/` 下的 unittest。
- `python tune.py smoke <候选编号> 408`：申请一次真实回放（408 最短）。结果写入 `candidates/<候选编号>/smoke.json`。排队期间请等待，不要重复申请。
"""


def write_code_map(workspace):
    (Path(workspace) / "digest").mkdir(parents=True, exist_ok=True)
    (Path(workspace) / "digest" / "code_map.md").write_text(CODE_MAP, encoding="utf-8")


# ---------------------------------------------------------------- series and results

def series(runs, tag):
    out = {}
    for workload, tags in (runs or {}).items():
        rows = tags.get(tag) or []
        if rows:
            out[workload] = dict(ttft=[r["ttft"] for r in rows], makespan=[r["makespan"] for r in rows])
    return out


def baseline_series(state, arm="light"):
    return {w: dict(ttft=list(v[arm]["ttft"]), makespan=list(v[arm]["makespan"]))
            for w, v in state["baseline"].items() if arm in v and v[arm]["ttft"]}


def reference_series(state):
    """What a candidate must beat: the champion's confirmed runs, or the baseline."""
    champion = state.get("champion")
    cand = state["candidates"].get(champion) if champion else None
    if cand:
        confirmed = series(cand.get("runs"), "confirm")
        if set(confirmed) == set(W):
            return confirmed, champion
    return baseline_series(state), None


def best_tag(cand):
    runs = cand.get("runs") or {}
    if all(len((runs.get(w) or {}).get("confirm") or []) >= 1 for w in W):
        return "confirm"
    if all(len((runs.get(w) or {}).get("screen") or []) >= 1 for w in W):
        return "screen"
    return None


def scored(state):
    """Every candidate with a full set of screen or confirm runs, with its result against the baseline."""
    base = baseline_series(state)
    if not base:
        return []
    out = []
    for cand_id, cand in state["candidates"].items():
        tag = best_tag(cand)
        if not tag:
            continue
        results = evaluator.workload_result(base, series(cand["runs"], tag))
        if set(results) != set(W):
            continue
        out.append(dict(id=cand_id, tag=tag, results=results, score=evaluator.total_score(results),
                        cand=cand))
    return sorted(out, key=lambda item: -(item["score"] if item["score"] is not None else -9))


def stress_results(state, cand):
    """Attack outcomes: the candidate against light on each stress workload it ran."""
    out = {}
    for stress, spec in config.STRESS_SPECS.items():
        light = state["stress_baseline"].get(stress, {}).get("light")
        rows = [r for tags in (cand.get("runs") or {}).values() for r in (tags.get("attack") or []) if r.get("stress") == stress]
        if not light or len(light["ttft"]) < 2 or not rows:
            continue
        key = next(w for w in tags_for_stress(cand, stress))
        out[stress] = dict(
            ttft=evaluator.improvement(evaluator.stats(light["ttft"]), evaluator.stats([r["ttft"] for r in rows])),
            makespan=evaluator.improvement(evaluator.stats(light["makespan"]), evaluator.stats([r["makespan"] for r in rows])),
            regresses=any(evaluator.regression(evaluator.stats(light[m]), evaluator.stats([r[m] for r in rows]))
                          for m in ("ttft", "makespan")), key=key)
    return out


def tags_for_stress(cand, stress):
    for workload, tags in (cand.get("runs") or {}).items():
        if any(r.get("stress") == stress for r in (tags.get("attack") or [])):
            yield workload


def leave_one_out(state, cand_id):
    """G4. Drop each workload in turn: does the candidate stay in the top three?"""
    rows = scored(state)
    if not rows:
        return True, {}
    report = {}
    for drop in W:
        ranking = sorted(rows, key=lambda item: -(evaluator.total_score({w: r for w, r in item["results"].items() if w != drop}) or -9))
        names = [item["id"] for item in ranking[:3]]
        report[drop] = cand_id in names
    return all(report.values()), report


# ---------------------------------------------------------------- digests for the agents

def _mean_digest(digests):
    """Average the scalar leaves of several run digests."""
    if not digests:
        return {}
    out = {}
    for key in ("overload", "workers", "permits", "waits", "read_wait_s", "model_sum_s", "reference", "admission_events"):
        samples = [d[key] for d in digests if key in d]
        if not samples:
            continue
        if isinstance(samples[0], dict):
            out[key] = {k: statistics.mean(s[k] for s in samples if k in s) for k in samples[0]
                        if all(isinstance(s.get(k), (int, float)) for s in samples)}
        else:
            out[key] = statistics.mean(samples)
    return out


def run_digest(daemon, run_id):
    try:
        return read_json(daemon.ws / "runs" / run_id / "result.json").get("digest") or {}
    except (OSError, ValueError):
        return {}


def digests_of(daemon, run_ids):
    return [digest for digest in (run_digest(daemon, run_id) for run_id in run_ids) if digest]


def workload_block(daemon, name, label, cand=None):
    """One workload's numbers for the analyst: the baseline, and the candidate if it ran here."""
    state = daemon.state
    base = state["baseline"].get(name, {}).get("light")
    if not base:
        return ""
    lines = ["### %s（`%s`）" % (label, name), ""]
    st, sm = evaluator.stats(base["ttft"]), evaluator.stats(base["makespan"])
    lines.append("默认策略（%d 轮）：平均 TTFT %.3f ± %.3f s，完成时间 %.3f ± %.3f s。" % (
        st["n"], st["mean"], st["sd"], sm["mean"], sm["sd"]))
    digests = digests_of(daemon, base["runs"])
    agg = _mean_digest(digests)
    if agg:
        o, w, ref = agg.get("overload", {}), agg.get("workers", {}), agg.get("reference", {})
        lines.append("默认策略的诊断：超载 %.2f s（按层读取计 %.2f s），饱和 %.2f s，读取拉伸中位 %.1f 倍 / P95 %.1f 倍，链路利用率 %.2f；"
                     "实例空转合计 %.1f s（其中有请求在等却空转 %.1f s），等 KV 合计 %.1f s。" % (
                         o.get("overload_s", 0), o.get("layer_overload_s", 0), o.get("saturated_s", 0),
                         o.get("read_stretch_median", 0), o.get("read_stretch_p95", 0), o.get("link_utilization", 0),
                         w.get("idle_s", 0), w.get("starved_idle_s", 0), agg.get("read_wait_s", 0)))
        lines.append("参考：无争用 FCFS 的平均 TTFT %.3f s、完成时间 %.3f s；计算下界 %.3f s，链路下界 %.3f s。" % (
            ref.get("mean_ttft_s", 0), ref.get("makespan_s", 0), ref.get("compute_floor_s", 0), ref.get("link_floor_s", 0)))
        # What the analyst could not see before: the classification line of ordinary TFS here,
        # and how the requests were spread over the four workers.
        tfs_runs = state["baseline"][name].get("tfs", {}).get("runs") or []
        tfs_permits = _mean_digest(digests_of(daemon, tfs_runs)).get("permits", {})
        if tfs_permits.get("theta_final_Bps"):
            lines.append("普通 TFS 在本负载的口径（参照轮次）：θ = %.2f GB/s，额度 %s，重读提示 %d 条。θ 是分类线，也是额度的分母；"
                         "它在每次请求到达时按已到达请求的 P75 重算，这里是结束时的值。" % (
                             tfs_permits["theta_final_Bps"] / 1e9, tfs_permits.get("cap_final"), tfs_permits.get("heavy_hints", 0)))
        first_digest = digests[0] if digests else {}
        loads = (first_digest.get("workers") or {}).get("per_worker") or []
        if loads:
            lines.append("四个实例各执行 %s 条请求，忙碌 %s 秒。" % (
                "、".join(str(int(x["requests"])) for x in loads), "、".join(num(x["busy_s"], 1) for x in loads)))
    first = digests[0] if digests else {}
    if first.get("classes"):
        lines += ["", table(["类（只用于诊断）", "条数", "平均到达 s", "重读占比", "平均 TTFT s", "P95 TTFT s", "平均需求 GB/s", "读取 GB"],
                            [[cls, v["n"], num(v.get("mean_arrival_s"), 2) if v.get("mean_arrival_s") is not None else "-",
                              ("%.0f%%" % (100 * v["heavy_share"])) if "heavy_share" in v else "-", num(v["mean_ttft_s"]),
                              num(v["p95_ttft_s"]), num(v["mean_pressure_GBs"], 2), num(v["read_GB"], 3)]
                             for cls, v in first["classes"].items()]), ""]
        lines += [_timeline_md(first), ""]
    if cand:
        runs = (cand.get("runs") or {}).get(name) or {}
        rows = runs.get("confirm") or runs.get("screen") or []
        if rows:
            res = evaluator.workload_result({name: dict(ttft=base["ttft"], makespan=base["makespan"])},
                                            {name: dict(ttft=[r["ttft"] for r in rows], makespan=[r["makespan"] for r in rows])})[name]
            lines.append("候选 `%s` 在本负载（%d 轮）：平均 TTFT %.3f s（%s），完成时间 %.3f s（%s）。" % (
                cand.get("id"), res["cand_n"], res["cand_ttft"], pct(res["ttft"]), res["cand_makespan"], pct(res["makespan"])))
            agg = _mean_digest(digests_of(daemon, [r["run"] for r in rows]))
            if agg:
                o, w = agg.get("overload", {}), agg.get("workers", {})
                lines.append("候选的诊断：超载 %.2f s，饱和 %.2f s，读取拉伸 P95 %.1f 倍，实例空转 %.1f s（有请求在等 %.1f s），"
                             "等 KV %.1f s，被挡请求 %d 个共 %.1f s。" % (
                                 o.get("overload_s", 0), o.get("saturated_s", 0), o.get("read_stretch_p95", 0),
                                 w.get("idle_s", 0), w.get("starved_idle_s", 0), agg.get("read_wait_s", 0),
                                 agg.get("permits", {}).get("blocked_requests", 0), agg.get("permits", {}).get("blocked_total_s", 0)))
                events = agg.get("admission_events") or {}
                if events.get("total"):
                    lines.append("候选每轮的账本事件约 %d 条（账本只保留 10000 条，超过即校验失败）。" % events["total"])
    return "\n".join(lines)


def _timeline_md(digest):
    """One line per second: in-flight demand against the link, and requests computing."""
    timeline = digest.get("timeline") or {}
    demand, active = timeline.get("demand_GBs") or [], timeline.get("active_requests") or []
    if not demand:
        return ""
    per_sec = [statistics.mean(demand[i:i + 4] or [0]) for i in range(0, len(demand), 4)]
    act = [statistics.mean(active[i:i + 4] or [0]) for i in range(0, len(active), 4)]
    cells = []
    for index, value in enumerate(per_sec):
        bar = "#" * min(30, int(value / 25 * 10))
        cells.append("%2d s 需求 %5.1f GB/s 执行 %3.1f %s" % (index, value, act[index] if index < len(act) else 0, bar))
    return "时间线（每秒一行，需求为在途请求的峰值需求之和；链路 25 GB/s）：\n\n```\n" + "\n".join(cells) + "\n```"


def write_baseline_digest(daemon):
    state = daemon.state
    lines = ["# 基线摘要", "", "默认策略（light）与普通 TFS，同一入口、同一机器。平均 TTFT 与完成时间都是客户端口径。", ""]
    rows = []
    for w in W:
        light = state["baseline"].get(w, {}).get("light")
        if not light:
            continue
        tfs = state["baseline"].get(w, {}).get("tfs", {})
        lt, lm = evaluator.stats(light["ttft"]), evaluator.stats(light["makespan"])
        tt = evaluator.stats(tfs["ttft"]) if tfs.get("ttft") else None
        tm = evaluator.stats(tfs["makespan"]) if tfs.get("makespan") else None
        agg = _mean_digest(digests_of(daemon, light["runs"]))
        ref = agg.get("reference", {})
        rows.append([config.WORKLOADS[w]["title"], lt["n"], "%.3f ± %.3f" % (lt["mean"], lt["sd"]), "%.3f ± %.3f" % (lm["mean"], lm["sd"]),
                     "%.3f ± %.3f" % (tt["mean"], tt["sd"]) if tt else "-", "%.3f ± %.3f" % (tm["mean"], tm["sd"]) if tm else "-",
                     num(ref.get("mean_ttft_s")), num(ref.get("makespan_s")), num(ref.get("compute_floor_s")), num(ref.get("link_floor_s"))])
    lines += [table(["负载", "轮数", "默认 平均TTFT s", "默认 完成时间 s", "普通TFS 平均TTFT s", "普通TFS 完成时间 s",
                     "无争用参考 TTFT", "无争用参考 完成", "计算下界", "链路下界"], rows), ""]
    lines += ["同机重复运行的波动：短请求负载的平均 TTFT 半宽约 ±2.5–6%，完成时间约 ±1–3%。任何单个数字都要放在这个波动里看。", "",
              "## 已知的反例（来自 lql_dev 的探索，作为先验）", "",
              "- measured 策略在 cap=1 时自锁：串行下按实际占用算出的中位数，得到的 cap 仍是 1。",
              "- 放宽 cap 在巨需求负载上有害：平均 TTFT 明显变差。",
              "- 固定 cap=1 让 408 的完成时间从约 12.2 s 变到 20.5 s。",
              "- lease 释放改回首 token 后更差。",
              "- 「排队」和「竞争」可以互相转换：限制并发会把竞争换成排队，两者都伤平均 TTFT；只有减少实例空转才能改善完成时间。", ""]
    (daemon.ws / "digest" / "baseline.md").write_text("\n".join(lines), encoding="utf-8")


def write_bounds(daemon):
    """Stage 0's ceiling references, for the dashboard and the agents."""
    state = daemon.state
    bounds = {}
    for w in W:
        light = state["baseline"].get(w, {}).get("light")
        if not light:
            continue
        agg = _mean_digest(digests_of(daemon, light["runs"]))
        ref, sm = agg.get("reference", {}), evaluator.stats(light["makespan"])
        floors = [v for v in (ref.get("compute_floor_s"), ref.get("link_floor_s")) if v]
        bounds[w] = dict(light_ttft=evaluator.stats(light["ttft"])["mean"], light_makespan=sm["mean"],
                         no_contention_ttft=ref.get("mean_ttft_s"), no_contention_makespan=ref.get("makespan_s"),
                         floor_makespan=max(floors) if floors else None)
    state["bounds"] = bounds


def write_champion_digest(daemon):
    state = daemon.state
    lines = ["# 当前冠军", ""]
    champion = state.get("champion")
    cand = state["candidates"].get(champion) if champion else None
    if not cand:
        lines += ["现在还没有冠军。参照对象是默认策略（light）。第一个冠军必须：排序分的 95% 置信下界高于默认策略，且任何负载上两个指标都没有超出噪声的回退。", ""]
    else:
        card = cand.get("card") or {}
        lines += ["冠军 `%s`（第 %d 代产生，类型 %s）。" % (champion, cand.get("gen", 0), "提案" if cand["kind"] == "llm" else "调参"), "",
                  "机制：%s。" % card.get("name", ""), "", "因果链：%s" % card.get("causal", ""), "",
                  "参数：" + "、".join("%s = %s" % (k, v.get("value")) for k, v in (card.get("params") or {}).items()), ""]
        base = baseline_series(state)
        res = evaluator.workload_result(base, series(cand.get("runs"), "confirm") or series(cand.get("runs"), "screen"))
        lines += [table(["负载", "冠军 平均TTFT s", "相对默认", "冠军 完成时间 s", "相对默认"],
                        [[config.WORKLOADS[w]["title"], num(r["cand_ttft"]), pct(r["ttft"]), num(r["cand_makespan"]), pct(r["makespan"])]
                         for w, r in res.items()]), "", "CARD 全文见 `candidates/%s/CARD.md`，代码见 `candidates/%s/overlay/`。" % (champion, champion), ""]
    (daemon.ws / "digest" / "champion.md").write_text("\n".join(lines), encoding="utf-8")


def write_context(daemon, generation, lab):
    state = daemon.state
    rows = scored(state)
    lines = ["# 第 %d 代上下文（提案组 %s）" % (generation, lab), ""]
    ladder = state.get("ladder", 0)
    lines += ["当前停滞阶梯：%d / %d — %s" % (ladder, len(config.LADDER) - 1, config.LADDER[min(ladder, len(config.LADDER) - 1)]),
              "已放开的第二梯队：%s" % ("、".join(config.TIER2[t]["name"] for t in sorted(config.TIER2_RUNG) if t <= open_tier_of(state)) or "无"), ""]
    lines += ["## 排行榜（相对默认策略）", ""]
    if rows:
        lines += [table(["候选", "类型", "轮次", "平均TTFT 改进", "完成时间 改进", "排序分", "状态"],
                        [["`%s`" % r["id"], r["cand"]["kind"], r["tag"], pct(_mean(r["results"], "ttft")), pct(_mean(r["results"], "makespan")),
                          pct(r["score"]), r["cand"].get("status")] for r in rows[:10]]), ""]
    else:
        lines += ["还没有任何候选跑完五个负载。", ""]
    meta = state["meta"]
    lines += ["## 经验教训", "", meta.get("lessons_md") or "（还没有）", "",
              "## 本代提示", "", "给提案人：%s" % (meta.get("proposer_hints") or "（无）"), "",
              "## 机制地图", ""]
    counts = map_counts(state)
    total = len(config.MECHANISM_CELLS)
    tried = sum(1 for c in config.MECHANISM_CELLS if counts.get(c))
    lines += ["已试 %d / %d 格。按「信号 × 动作」统计已试格数（每格含 时机 × 反馈 共 8 小格）：" % (tried, total), ""]
    lines += [table(["信号 \\ 动作"] + list(config.ACTIONS),
                    [[s] + [sum(1 for t in config.TIMINGS for f in config.FEEDBACKS if counts.get("%s/%s/%s/%s" % (s, a, t, f)))
                            for a in config.ACTIONS] for s in config.SIGNALS]), ""]
    untested = [c for c in config.MECHANISM_CELLS if not counts.get(c)]
    lines += ["完全没碰过的格子（最多列 40 个）：" + "、".join(untested[:40]) if untested else "机制地图上已没有未试格子。", ""]
    prover = daemon.ws / "digest" / ("gen_%d" % generation) / "untested.json"
    if prover.exists():
        try:
            lines += ["举证者认为值得优先尝试的方向：", "", "```", json.dumps(json.loads(prover.read_text(encoding="utf-8")), ensure_ascii=False, indent=1)[:6000], "```", ""]
        except ValueError:
            pass
    lens = roles_lens(state, lab)
    lines += ["## 本代的视角", "", "提案组 %s 本代优先从「%s」出发思考。这只是起点，不限制你。" % (lab, lens), ""]
    lines += operator_notes(state)
    (daemon.ws / "digest" / ("gen_%d" % generation)).mkdir(parents=True, exist_ok=True)
    (daemon.ws / "digest" / ("gen_%d" % generation) / ("context_%s.md" % lab)).write_text("\n".join(lines), encoding="utf-8")
    return "digest/gen_%d/context_%s.md" % (generation, lab)


def _mean(results, key):
    return statistics.mean(r[key] for r in results.values())


def operator_notes(state):
    """What the operator changed about the measuring conditions, for every role that reads results.

    ``state['operator_notes']`` is a list of {at, text}; the daemon never writes it.
    """
    notes = (state.get("operator_notes") or [])[-5:]
    if not notes:
        return []
    return ["## 运维备注（测量条件的变化）", ""] + ["- %s：%s" % (note.get("at", ""), note.get("text", "")) for note in notes] + [""]


def open_tier_of(state):
    rung = int(state.get("ladder") or 0)
    return max([t for t, at in config.TIER2_RUNG.items() if rung >= at] or [0])


def roles_lens(state, lab):
    """The lens one proposal group starts from in a generation. Groups of one generation never share a lens."""
    labs = roles.LABS
    index = (state["gen"]["n"] * len(labs) + labs.index(lab)) % len(roles.LENSES)
    return roles.LENSES[index]


def map_counts(state):
    return {cell: len(ids) for cell, ids in (state.get("map") or {}).items() if ids}


def write_run_digests(daemon, generation):
    """The analyst's input: each workload's numbers, baseline against the current champion."""
    state = daemon.state
    champion = state["candidates"].get(state.get("champion")) if state.get("champion") else None
    lines = ["# 逐负载诊断输入（第 %d 代）" % generation, "",
             "数字由程序从运行记录算出。时间轴是其中一轮的，用于看形状；均值才是结论。", ""]
    for w in W:
        lines += [workload_block(daemon, w, config.WORKLOADS[w]["title"], champion), ""]
    folder = daemon.ws / "digest" / ("gen_%d" % generation)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run_digests.md").write_text("\n".join(lines), encoding="utf-8")


EVALUATION_RULES = """## 评价规则（冻结，不能修改；写在这里，免得靠猜）

参照对象：没有冠军时是默认策略的串行基线；有冠军后，晋升比较的是冠军的确认轮次。改进 =（参考均值 − 候选均值）/ 参考均值，正数是改进。
- 每个负载的得分 = 0.5 × 平均 TTFT 改进 + 0.5 × 完成时间改进。
- 排序分 = 0.5 ×（各负载得分的平均值）+ 0.5 ×（最差负载的得分）。
- 筛选（每个负载 %(screen)d 轮）：五个负载的平均 TTFT 改进 > 0 且平均完成时间改进 > 0，并且任何一个负载上任何一个指标不低于 -10%%。
- 确认（每个负载至少 %(confirm)d 轮，分不出高低时加到 %(most)d 轮）：排序分 > 0，并且任何一个负载上任何一个指标不低于 -5%%。
- 晋升：排序分的 95%% 单侧下界 > 0，并且没有任何负载上任何指标出现超出噪声的回退。回退 = 候选均值 − 参考均值 > max(0.5%% × 参考均值, %(z).3f × 标准误)。
- 晋升前另有压力负载（%(stress)d 个派生负载上不得比默认策略差）和留一法；候选在机制地图上承诺的诊断量要兑现。
"""

PARALLEL_EVENTS = ("stage0_parallel_decided", "manual_parallel")


def _ledger_events(ws):
    events = []
    try:
        with (Path(ws) / "ledger.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def parallel_levels(ws):
    """{run id: how many replays shared the host when that run started}.

    Baselines are measured one replay at a time. A result measured beside another replay is not
    comparable with them on every workload, so the report says which rows that applies to. The level
    is 1 unless the ledger records a change (the stage-0 decision, or the operator's) before the start.
    """
    events = _ledger_events(ws)
    changes = [(e["ts"], int(e.get("parallel") or 1)) for e in events if e["kind"] in PARALLEL_EVENTS]
    levels = {}
    for event in events:
        if event["kind"] != "run_started":
            continue
        level = 1
        for ts, value in changes:
            if ts <= event["ts"]:
                level = value
        levels[event["run"]] = level
    return levels


def mechanism_text(card):
    """('信号 × 动作 · 时机 · 反馈', '参数取值') of a candidate card."""
    card = card if isinstance(card, dict) else {}
    mechanism = card.get("mechanism") if isinstance(card.get("mechanism"), dict) else {}

    def join(axis):
        value = mechanism.get(axis)
        if isinstance(value, list):
            return "/".join(str(item) for item in value) or "?"
        return str(value) if value else "?"

    params = card.get("params") if isinstance(card.get("params"), dict) else {}
    values = ", ".join("%s=%s" % (name, spec.get("value") if isinstance(spec, dict) else spec) for name, spec in params.items())
    return "%s × %s · %s · %s" % (join("signal"), join("action"), join("timing"), join("feedback")), values or "无"


def _clip(text, limit=900):
    return str(text or "（卡片里没有写）").replace("\n", " ").strip()[:limit] or "（卡片里没有写）"


def write_generation_report(daemon):
    """Facts of one generation for the meta-strategist; written by the program, not by a model."""
    from .store import read_json
    state = daemon.state
    generation = state["gen"]["n"]
    lines = ["# 第 %d 代报告" % generation, ""]
    levels = parallel_levels(daemon.ws)
    mine = [cand for cand in state["candidates"].values() if cand.get("gen") == generation]
    rows = []
    for cand in mine:
        mechanism, params = mechanism_text(cand.get("card"))
        rows.append([cand["id"], cand["kind"], (cand.get("card") or {}).get("name", ""), mechanism, params, cand.get("status"),
                     pct(cand.get("score")), cand.get("reason") or ""])
    lines += [table(["候选", "类型", "名称", "机制：信号 × 动作 · 时机 · 反馈", "参数", "状态", "排序分", "原因"], rows)
              or "（本代没有候选）", ""]
    # Where each candidate helped and where it hurt. The reason above is one sentence; the lessons
    # of the search are read off these numbers, so they must be in the report and not only on the dashboard.
    per_workload = [row for row in scored(state) if row["cand"].get("gen") == generation]
    lines += ["## 本代候选的逐负载结果", "",
              "相对默认策略，正数是改进。取该候选轮次最多的一组回放（确认优先于筛选）；「回退」表示超出噪声。",
              "「并行度」是该负载这几轮回放启动时同时占着主机的回放数：大于 1 的行与串行基线不完全可比（408 最敏感）。", ""]
    for row in per_workload:
        cand = row["cand"]
        measured = []
        for workload, result in row["results"].items():
            runs = ((cand.get("runs") or {}).get(workload) or {}).get(row["tag"]) or []
            measured.append([workload, pct(result["ttft"]), pct(result["makespan"]), result["cand_n"],
                             max([levels.get(item["run"], 1) for item in runs] or [1]), "是" if result["regresses"] else ""])
        lines += ["### %s（%s）" % (row["id"], (cand.get("card") or {}).get("name", "")), ""]
        lines += [table(["负载", "平均TTFT 改进", "完成时间 改进", "轮次", "并行度", "回退"], measured), ""]
    if not per_workload:
        lines += ["本代没有跑完五个负载的候选。", ""]
    described = [cand for cand in mine if cand.get("card")]
    if described:
        lines += ["## 本代候选的卡片自述（提案人写的，不是结论）", ""]
        for cand in described:
            card = cand["card"]
            lines += ["### %s（%s）" % (cand["id"], card.get("name", "")), "",
                      "原因链：%s" % _clip(card.get("causal")), "",
                      "预设的失败形态：%s" % _clip(card.get("failure")), "",
                      "全文见 `candidates/%s/CARD.md`。" % cand["id"], ""]
    lines += operator_notes(state)
    lines += [EVALUATION_RULES % dict(screen=config.SCREEN_RUNS, confirm=config.CONFIRM_RUNS, most=config.MAX_CONFIRM_RUNS,
                                      z=evaluator.Z95, stress=len(config.STRESS_SPECS))]
    lines += ["冠军：%s。停滞阶梯：%d。已放开到第二梯队第 %d 档。" % (
        state.get("champion") or "无", state.get("ladder", 0), open_tier_of(state)), ""]
    kinds = state.get("runs_by_kind", {})
    total = kinds.get("llm", 0) + kinds.get("param", 0) or 1
    lines += ["运行预算：新机制候选占 %.0f%%，调参候选占 %.0f%%（保底线 %.0f%%）。下一代的新机制候选数就是你修改单里的 "
              "allocation.llm（1–%d，默认 2）：每个提案组出一个，各组同时工作、视角不同、互不知晓。调参候选（allocation.param，0–4）"
              "是有冠军之后，冠军改一两个参数得到的子候选。" % (
                  100 * kinds.get("llm", 0) / total, 100 * kinds.get("param", 0) / total, 100 * config.EXPLORE_MIN,
                  len(roles.LABS)), ""]
    tasks = [(t, r) for t, r in state["tasks"].items() if "gen%d" % generation in t or ("_%d_" % generation) in t]
    if tasks:
        lines += [table(["任务", "角色", "状态", "用时 min", "步数"], [["`%s`" % t, r.get("role"), r.get("status"), num((r.get("duration_s") or 0) / 60, 1), r.get("steps")] for t, r in tasks]), ""]
    lines += ["## 上一代的经验教训（供你修订）", "", state["meta"].get("lessons_md") or "（还没有）", ""]
    lines += ["## 假设库（供你修订）", "", "```", json.dumps(state["meta"].get("hypotheses") or [], ensure_ascii=False, indent=1), "```", ""]
    folder = daemon.ws / "generations" / ("gen_%d" % generation)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return "generations/gen_%d/report.md" % generation


# ---------------------------------------------------------------- dashboard

STYLE = """body{font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#0f1419;color:#dfe6ee}
header{display:flex;gap:18px;align-items:center;padding:12px 20px;background:#151b23;border-bottom:1px solid #263241;flex-wrap:wrap}
h1{font-size:18px;margin:0} h2{font-size:15px;margin:18px 0 6px;color:#8fb8ff}
main{padding:12px 20px 40px} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}
.card{background:#151b23;border:1px solid #263241;border-radius:8px;padding:12px 14px}
table{border-collapse:collapse;width:100%} th,td{padding:3px 8px;text-align:right;border-bottom:1px solid #222b36;white-space:nowrap}
th:first-child,td:first-child{text-align:left} th{color:#93a4b8;font-weight:600}
.pos{color:#5ad18f} .neg{color:#ff7a7a} .muted{color:#7d8b9d} .warn{color:#ffcf5c}
.kpi{font-size:22px;font-weight:600} .lbl{color:#93a4b8;font-size:12px}
.mapg{display:grid;grid-template-columns:repeat(9,1fr);gap:3px;font-size:11px} .mapg div{background:#1b232e;padding:3px 4px;text-align:center;border-radius:3px}
a{color:#8fb8ff} pre{white-space:pre-wrap;font-size:12px;background:#0b0f14;padding:8px;border-radius:6px;max-height:260px;overflow:auto}
"""


def _class(value):
    return "pos" if value > 0.005 else ("neg" if value < -0.005 else "muted")


class Dashboard:
    def __init__(self, daemon):
        self.d = daemon
        self.state = daemon.state

    def render(self):
        state, d = self.state, self.d
        elapsed = _now() - state.get("created", _now())
        champion = state["candidates"].get(state.get("champion")) if state.get("champion") else None
        base = baseline_series(state)
        head = "TFS 自动调优看板 · 第 %d 代 · 已运行 %s · %s" % (
            state["gen"]["n"], _dur(elapsed), {"run": "运行中", "stage0": "第 0 代准备", "paused": "已暂停", "stopping": "正在收尾",
                                               "stopped": "已停止", "needs_human": "需要人工处理"}.get(state["phase"], state["phase"]))
        parts = [head_html(head, state, champion), '<div class="grid">']
        parts.append(card("相对默认策略的改进（冠军）", improvements_table(state, champion, base)))
        parts.append(card("用量与健康", usage_table(state, d)))
        parts.append(card("改进趋势（排序分，按代）", trend_svg(state)))
        parts.append(card("机制地图：信号 × 动作（数字是已试的小格数，满格 8）", map_html(state)))
        parts.append(card("排行榜（前 10，相对默认策略）", leaderboard_table(state)))
        parts.append(card("正在运行与最近结果", live_table(state, d)))
        parts.append(card("停滞阶梯与举证", ladder_html(state, d)))
        parts.append(card("提示与告警", alerts_html(state)))
        parts.append("</div>")
        link = lambda text, path: '<a href="%s">%s</a>' % (path, text)  # noqa: E731
        rendered = _now()
        parts.append('<p class="muted">更新于 %s · %s · %s · %s · 刷新：每 60 秒</p>' % (
            time.strftime("%H:%M:%S", time.localtime(rendered)), link("账本 ledger.jsonl", "../ledger.jsonl"),
            link("代码地图", "../digest/code_map.md"), link("当前冠军", "../digest/champion.md")))
        # The page only changes while the daemon renders it. A daemon that is down, or a supervisor that
        # gave up on it, leaves the last page standing: say so on the page itself, however it was loaded.
        page = "<!doctype html><html lang='zh'><head><meta charset='utf-8'><meta http-equiv='refresh' content='60'>" \
               "<title>TFS 自动调优看板</title><style>%s</style></head><body>%s%s</body></html>" % (
                   STYLE, "\n".join(parts), STALE_SCRIPT.replace("RENDERED_MS", str(int(rendered * 1000))))
        out = d.ws / "dashboard" / "index.html"
        tmp = out.with_name("index.html.tmp")
        tmp.write_text(page, encoding="utf-8")
        tmp.replace(out)


STALE_SCRIPT = """<script>
(function () {
  var staleMinutes = Math.floor((Date.now() - RENDERED_MS) / 60000);
  if (staleMinutes < 5) return;
  var box = document.createElement("div");
  box.style.cssText = "border:1px solid #ff7a7a;padding:10px 14px;margin:10px 0;color:#ff7a7a";
  box.textContent = "看板已经 " + staleMinutes + " 分钟没有更新：守护进程可能已经停止。" +
    "查看工作区里的 SUPERVISOR_STOPPED.txt、NEEDS_HUMAN.md 和 daemon.log。";
  document.body.insertBefore(box, document.body.firstChild);
})();
</script>"""


def _now():
    import time
    return time.time()


def _dur(seconds):
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return ("%d 天 " % days if days else "") + "%d 小时 %d 分" % (hours, rest // 60)


def card(title, body):
    return "<div class='card'><h2>%s</h2>%s</div>" % (html.escape(title), body)


def head_html(head, state, champion):
    alerts = [a for a in state.get("alerts", []) if a["level"] in ("warn", "error")]
    text = "<h1>%s</h1><span class='lbl'>冠军</span> <b>%s</b> <span class='muted'>%s</span>" % (
        html.escape(head), html.escape(state.get("champion") or "无"),
        html.escape(((champion or {}).get("card") or {}).get("name", "")) if champion else "")
    if state.get("needs_human"):
        text += "<div class='neg'>需要人工处理：%s</div>" % html.escape(state["needs_human"])
    if alerts:
        text += "<div class='warn'>最近告警：%s</div>" % html.escape(alerts[-1]["text"])
    if state["env"].get("waiting_reason"):
        text += "<div class='warn'>环境：%s，自 %s 等待中</div>" % (html.escape(state["env"]["waiting_reason"]), state["env"].get("waiting_since"))
    return "<header>%s</header>" % text


def improvements_table(state, champion, base):
    rows = []
    bounds = state.get("bounds") or {}
    if champion:
        res = evaluator.workload_result(base, series(champion.get("runs"), "confirm") or series(champion.get("runs"), "screen"))
        for w in W:
            r = res.get(w)
            b = bounds.get(w, {})
            rows.append("<tr><td>%s</td><td>%s</td><td class='%s'>%s</td><td>%s</td><td class='%s'>%s</td><td>%s</td></tr>" % (
                html.escape(config.WORKLOADS[w]["title"]), num(r["cand_ttft"]) if r else "-", _class(r["ttft"] if r else 0),
                pct(r["ttft"]) if r else "-", num(r["cand_makespan"]) if r else "-", _class(r["makespan"] if r else 0),
                pct(r["makespan"]) if r else "-",
                "参考 %s / %s" % (num(b.get("no_contention_ttft"), 2), num(b.get("no_contention_makespan"), 2)) if b else "-"))
    return ("<table><tr><th>负载</th><th>冠军 TTFT s</th><th>改进</th><th>冠军 完成 s</th><th>改进</th><th>无争用参考</th></tr>%s</table>"
            % "".join(rows) if rows else "<p class='muted'>还没有冠军。参照对象是默认策略。</p>")


def usage_table(state, daemon):
    kinds = state.get("runs_by_kind", {})
    total = kinds.get("llm", 0) + kinds.get("param", 0)
    glm, other = state["tokens"].get("glm", {}), state["tokens"].get("kimi", {})
    health = state["env"].get("health") or {}
    crossval = ("已停用" if not config.CROSSVAL_ENABLED else
                "第 %d 代用了 %d 次 / 上限 %d（%s 的独立会话）" % (
                    state["crossval"]["gen"], state["crossval"]["used"], config.CROSSVAL_PER_GENERATION,
                    config.MODELS["crossval"].rsplit("/", 1)[-1].split("#")[0]))
    return "<table>%s</table>" % "".join(
        "<tr><td class='lbl'>%s</td><td>%s</td><td class='lbl'>%s</td><td>%s</td></tr>" % (pair[0][0], pair[0][1], pair[1][0], pair[1][1])
        for pair in [
        (("运行轮次", "%d（成功 %d，失败 %d）" % (state["runs_total"], state["runs_ok"], state["runs_failed"])),
         ("并行度（筛选）", "%d" % state["parallel"])),
        (("GLM token", "%.2f 亿（%d 次调用）" % (sum(glm.get(k, 0) for k in ("input", "output", "reasoning")) / 1e8, glm.get("calls", 0))),
         ("交叉验证", crossval)),
        (("探索 / 调参（轮次）", "%.0f%% / %.0f%%（保底 %.0f%%）" % (
            100 * kinds.get("llm", 0) / (total or 1), 100 * kinds.get("param", 0) / (total or 1), 100 * config.EXPLORE_MIN)),
         ("非 GLM 模型 token", "%.0f 万" % (sum(other.get(k, 0) for k in ("input", "output", "reasoning")) / 1e4))),
        (("WSL", "正常" if health.get("ok") else ("异常：%s" % health.get("reason"))),
         ("漂移", "对照连续偏离 %d 次" % state.get("drift_strikes", 0))),
        (("排队 / 在跑", "%d / %d" % (len(state["queue"]), len(state["active"]))),
         ("运行目录占用", html.escape(daemon.state["env"].get("disk", "?")))),
    ])


def trend_svg(state):
    history = state.get("history") or []
    width, height, pad = 560, 170, 34
    if len(history) < 2:
        return "<p class='muted'>完成两代后显示趋势。</p>"
    values = [100 * (h.get("best_score") or 0) for h in history]
    lo, hi = min(0, min(values)), max(1, max(values))
    x = lambda i: pad + (width - 2 * pad) * i / (len(values) - 1)  # noqa: E731
    y = lambda v: height - pad - (height - 2 * pad) * (v - lo) / (hi - lo)  # noqa: E731
    points = " ".join("%.1f,%.1f" % (x(i), y(v)) for i, v in enumerate(values))
    return "<svg viewBox='0 0 %d %d' width='100%%' style='max-width:620px'><polyline fill='none' stroke='#5ad18f' stroke-width='2' points='%s'/>%s%s</svg>" % (
        width, height, points,
        "".join("<circle cx='%.1f' cy='%.1f' r='3' fill='#5ad18f'/><text x='%.1f' y='%.1f' fill='#93a4b8' font-size='11' text-anchor='middle'>%s</text>"
                % (x(i), y(v), x(i), y(v) - 8, ("第%d代" % h["gen"]) if i in (0, len(values) - 1) or h.get("promoted") else "")
                for i, (v, h) in enumerate(zip(values, history))),
        "<text x='%d' y='%d' fill='#93a4b8' font-size='11'>0%%</text>" % (6, y(0) + 4))


def map_html(state):
    counts = map_counts(state)
    cells = ["<div class='muted'>信号 \\ 动作</div>"] + ["<div class='muted'>%s</div>" % a for a in config.ACTIONS]
    for s in config.SIGNALS:
        cells.append("<div class='muted' style='text-align:left'>%s</div>" % s)
        for a in config.ACTIONS:
            n = sum(1 for t in config.TIMINGS for f in config.FEEDBACKS if counts.get("%s/%s/%s/%s" % (s, a, t, f)))
            shade = int(40 + 150 * n / 8)
            cells.append("<div style='color:rgb(%d,225,%d)'>%d</div>" % (60 + 180 * n / 8, 140 - 60 * n / 8, n))
    return "<div class='mapg'>%s</div><p class='muted'>已试 %d / %d 格</p>" % (
        "".join(cells), sum(1 for c in config.MECHANISM_CELLS if counts.get(c)), len(config.MECHANISM_CELLS))


def leaderboard_table(state):
    rows = scored(state)
    if not rows:
        return "<p class='muted'>还没有跑完五个负载的候选。</p>"
    return "<table><tr><th>候选</th><th>类型</th><th>轮次</th><th>平均TTFT</th><th>完成时间</th><th>排序分</th><th>状态</th></tr>%s</table>" % "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td><td class='%s'>%s</td><td class='%s'>%s</td><td>%s</td></tr>" % (
            html.escape(r["id"]), r["cand"]["kind"], r["tag"], _class(_mean(r["results"], "ttft")), pct(_mean(r["results"], "ttft")),
            _class(_mean(r["results"], "makespan")), pct(_mean(r["results"], "makespan")), _class(r["score"] or 0), pct(r["score"]),
            html.escape(str(r["cand"].get("status")))) for r in rows[:10])


def live_table(state, daemon):
    rows = []
    for run_id, item in sorted(state["active"].items(), key=lambda kv: kv[1]["started"]):
        rows.append("<tr><td>%s</td><td>%s</td><td>槽位 %d</td><td>%s</td></tr>" % (run_id, item.get("workload", "-"), item["slot"], _dur(_now() - item["started"])))
    last = state["dashboard"].get("last_run") or {}
    recent = [e for e in daemon.store.events("run_finished", reverse=True, limit=12)]
    body = "<table><tr><th>运行</th><th>负载</th><th>槽位</th><th>已用时</th></tr>%s</table>" % "".join(rows) if rows else "<p class='muted'>现在没有在跑的轮次。</p>"
    body += "<table><tr><th>最近完成</th><th>负载</th><th>标签</th><th>候选</th><th>TTFT</th><th>完成</th><th>状态</th></tr>%s</table>" % "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>" % (
            e.get("run"), e.get("workload"), e.get("tag"), e.get("cand") or "-", num(e.get("ttft")), num(e.get("makespan"), 2),
            "pos" if e.get("status") == "done" else "neg", html.escape(str(e.get("status"))) + (": " + html.escape(str(e.get("error"))[:80]) if e.get("error") else ""))
        for e in recent)
    return body


def ladder_html(state, daemon):
    rung = min(state.get("ladder", 0), len(config.LADDER) - 1)
    items = "".join("<div class='%s'>%s %s</div>" % ("" if i == rung else "muted", "▶" if i == rung else "·", html.escape(text))
                    for i, text in enumerate(config.LADDER))
    tiers = "、".join(config.TIER2[t]["name"] for t in sorted(config.TIER2_RUNG) if t <= open_tier_of(state)) or "无"
    dossier = (daemon.ws / "meta" / "exhaustion.md")
    counts = map_counts(state)
    untested = sum(1 for c in config.MECHANISM_CELLS if not counts.get(c))
    return "<p>连续未更替冠军：%d 代（满 %d 代升一级）。已放开第二梯队：%s。停滞阶梯：第 %d 级。</p><pre>%s</pre><p>机制地图未试格：%d。假设库：%d 条。%s</p>" % (
        state.get("stagnation", 0), config.STAGNATION_GENERATIONS, tiers, state.get("ladder", 0), items, untested,
        len(state["meta"].get("hypotheses") or []), "枯竭档案已生成，见 meta/exhaustion.md。" if dossier.exists() else "尚未生成枯竭档案。")


def alerts_html(state):
    alerts = state.get("alerts") or []
    return "<pre>%s</pre>" % html.escape("\n".join("%s [%s] %s" % (a["at"], a["level"], a["text"]) for a in alerts[-12:])) if alerts else "<p class='muted'>没有告警。</p>"
