#!/usr/bin/env python3
"""从固定 Git 与归档编写 Archify 合并报告，不运行仿真。"""

import argparse, csv, hashlib, json, shutil, statistics, subprocess
from collections import defaultdict
from datetime import datetime
from fractions import Fraction as F
from pathlib import Path
from zoneinfo import ZoneInfo

R = Path(__file__).resolve().parents[2]
P = R / "dynamo_vllm_cpu_sim"
A = P / "results/sa_alignment_summary_20261019"
V = P / "results/sa_code_review_20261019"
REV = "607c7eb28e6cf79afb906cd1bad50d4c72f0e512"


def read(p):
    return list(csv.DictReader(p.open()))


_table_sequence = 0


def table(h, rs):
    global _table_sequence
    _table_sequence += 1
    rs = [
        [str(r[0]) + f" {{#row-{_table_sequence}-{i}}}", *r[1:]]
        for i, r in enumerate(rs)
    ]

    def c(x):
        return str(x).replace("|", "\\|").replace("\n", " ")

    return "\n".join(
        [
            "| " + " | ".join(map(c, h)) + " |",
            "| " + " | ".join(["---"] * len(h)) + " |",
        ]
        + ["| " + " | ".join(map(c, r)) + " |" for r in rs]
    )


def pct(x):
    return f"{float(x) * 100:.1f}%"


def n(x):
    return f"{float(x):.2f}"


def write(p, rs):
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rs[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", type=Path)
    arg = ap.parse_args()
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    d = (arg.folder or R / ".archify" / f"explainer-tfs-sa-{stamp}").resolve()
    d.mkdir(parents=True, exist_ok=True)
    S = read(A / "summary.csv")
    U = read(A / "runs.csv")
    C = read(A / "classes.csv")
    B = read(A / "per_seed.csv")
    SA = read(A / "sa_controls.csv")
    I = read(V / "changed-files.csv")
    assert [len(S), len(U), len(C), len(I)] == [16, 96, 288, 208]
    # 独立重算全部请求的 TTFT、期限达标率与双层中位数。
    g = defaultdict(list)
    for r in read(A / "requests.csv"):
        g[tuple(r[k] for k in ("variant", "seed", "cap", "mode", "repeat"))].append(r)
    sg = defaultdict(list)
    for r in U:
        rs = g[tuple(r[k] for k in ("variant", "seed", "cap", "mode", "repeat"))]
        assert len(rs) == 360
        slo = sum(float(x["first_token_s"]) <= float(x["deadline_s"]) for x in rs) / 360
        ttft = (
            statistics.mean(
                float(x["first_token_s"]) - float(x["arrival_s"]) for x in rs
            )
            * 1000
        )
        assert (
            abs(slo - float(r["slo"])) < 1e-12
            and abs(ttft - float(r["ttft_ms"])) < 1e-9
        )
        sg[tuple(r[k] for k in ("variant", "seed", "cap", "mode"))].append(
            {"slo": slo, "ttft_ms": ttft}
        )
    gg = defaultdict(list)
    for (v, s, c, m), rs in sg.items():
        assert len(rs) == 3
        gg[(v, c, m)].append(
            {k: statistics.median(x[k] for x in rs) for k in ("slo", "ttft_ms")}
        )
    for r in S:
        for k in ("slo", "ttft_ms"):
            assert (
                abs(
                    float(r[k])
                    - statistics.median(
                        x[k] for x in gg[(r["variant"], r["cap"], r["mode"])]
                    )
                )
                < 1e-9
            )
    for metric, name, scale in [
        ("slo", "attainment.csv", 100),
        ("ttft_ms", "ttft.csv", 1),
    ]:
        chart = []
        for v in "AB":
            for cap in ("1", "8"):
                row = {"case": f"C_{v} / 上限{cap}"}
                for mode in ("immediate", "slack_only", "quota_fcfs", "tfs"):
                    row[mode] = (
                        float(
                            next(
                                r
                                for r in S
                                if r["variant"] == v
                                and r["cap"] == cap
                                and r["mode"] == mode
                            )[metric]
                        )
                        * scale
                    )
                chart.append(row)
        write(d / name, chart)
    (d / "assets").mkdir(exist_ok=True)
    for f in [
        "sa_alignment_A_timeline.png",
        "sa_alignment_B_timeline.png",
        "sa_alignment_ttft_spread.png",
    ]:
        shutil.copy2(R / "docs/figures/sa_alignment_20261019" / f, d / "assets" / f)
    sources = {
        "input": ("官方输入导出", "scripts/export_sa_e26f.py", "48:73"),
        "arrivals": ("已到达请求画像与路由提交", "sim/sa_aligned.py", "145:164"),
        "admission": ("排序、配额与占用", "sim/admission.py", "94:128"),
        "zero": ("SA零门槛规则", "sim/sa_aligned.py", "33:48"),
        "router": ("真实Dynamo路由与记账", "sim/router.py", "57:82"),
        "connector": ("同步外部前缀Connector", "sim/sa_layer_engine.py", "11:23"),
        "schedule": ("真实Scheduler输出与模拟step", "sim/sa_layer_engine.py", "58:96"),
        "layers": ("层等待、计算和一层预取", "sim/sa_layer_engine.py", "105:135"),
        "model": ("SA画像与独占参考时间", "sim/sa_model.py", "10:26"),
        "storage": ("聚合层流与max_min", "sim/sa_model.py", "60:80"),
        "feedback": ("首token与完成反馈", "sim/sa_aligned.py", "116:135"),
        "lease": ("准入占用释放", "sim/admission.py", "166:181"),
        "summary": (
            "16行完整策略汇总",
            "results/sa_alignment_summary_20261019/summary.csv",
            "1:17",
        ),
        "seed": (
            "逐种子结果与重复范围",
            "results/sa_alignment_summary_20261019/per_seed.csv",
            "1:33",
        ),
        "sa": (
            "8次SA现场控制",
            "results/sa_alignment_summary_20261019/sa_controls.csv",
            "1:9",
        ),
        "classes": (
            "288行逐次分类数据",
            "results/sa_alignment_summary_20261019/classes.csv",
            "1:289",
        ),
        "runs": (
            "96次完整运行汇总",
            "results/sa_alignment_summary_20261019/runs.csv",
            "1:97",
        ),
        "audit": (
            "2026-10-19复核证据",
            "results/sa_code_review_20261019/audit.json",
            "1:52",
        ),
        "tests": (
            "2026-10-19单元测试日志",
            "results/sa_code_review_20261019/unit-tests.log",
            "50:58",
        ),
        "parity": (
            "8个独立SA对拍案例",
            "results/sa_alignment_summary_20261019/verification.json",
            "1:114",
        ),
        "inventory": (
            "208个变更文件及SHA256",
            "results/sa_code_review_20261019/changed-files.csv",
            "1:209",
        ),
    }
    out = []

    def add(x):
        out.append(x.strip())

    def panel(t, id):
        add(f"## {t} {{#{id} span=12}}")

    add('''# TFS 代码改动与 SA 对齐实验报告 {#tfs-sa-report}

真实 vLLM 保留组批决策。新增桥接层把 step 接入 SA 逐层读算模型。归档支持 TFS 改善直接提交，但不支持逐场景复现 SA。

- 上半部分审代码，下半部分看实验。
- 真实调度与模拟执行分开说明。
- 保留全部16组结果和seed418反例。
- 使用2026-10-19归档，本次不重跑实验。

@answer scope="CPU仿真归档；顺序图只示依赖；报告生成于2026-10-20"''')
    add(
        f"@meta locale=zh-CN style=clean repo_url=https://github.com/QiliangLi/dynamo_vllm_simulation.git repo_revision={REV}"
    )
    for id, (title, path, span) in sources.items():
        path = "dynamo_vllm_cpu_sim/" + path
        content = subprocess.check_output(
            ["git", "show", f"{REV}:{path}"], cwd=R
        ).decode()
        a, b = map(int, span.split(":"))
        assert 0 < a <= b <= len(content.splitlines())
        add(
            f"@source {id} title={json.dumps(title, ensure_ascii=False)} path={path} lines={span}"
        )
    add(
        '@source original title="原实验结果与结论限制" path="SA-E26f对齐实验结果-20261019.md" lines=1:135'
    )
    panel("代码改动｜四类工作分别改了什么？", "code-overview")
    add(
        table(
            ["类别", "修改前", "修改后", "边界"],
            [
                [
                    "输入请求",
                    "自建负载或旧演示输入",
                    "直接导出SA官方360请求",
                    "保留h/u、到达、T0和期限",
                ],
                [
                    "TFS请求准入",
                    "到达后直接路由",
                    "中央等待、排序、重流配额、首token释放",
                    "决定何时进入路由",
                ],
                [
                    "vLLM组step接入",
                    "整段外部KV完成后恢复",
                    "同步前缀匹配，真实Scheduler决定token",
                    "未修改上游组批算法",
                ],
                [
                    "读算与反馈",
                    "整段读取、线性计算时长",
                    "SA八层读算、聚合存储竞争和反馈",
                    "时间仍由仿真产生",
                ],
            ],
        )
    )
    add(
        "四类是功能归纳。运行时涉及9个文件：7个新增、2个修改。另有实验、验收和报告工具。[](source:inventory)"
    )
    add(
        "[查看调用流程](block:code-path) · [跳到实验报告](block:experiment-start) · [查看16组结果](block:experiment-all)"
    )
    panel("代码改动｜真实代码和仿真各管哪一步？", "code-path")
    add("""@architecture id=execution-path title="从官方请求到执行结果"
| input-node | 官方输入 | external | source:input |
| admission-node | TFS准入 | backend | source:admission |
| routing-node | Dynamo路由 | backend | source:router |
| step-node | vLLM组step | backend | source:schedule |
| execution-node | 逐层读算 | backend | source:layers |
| input-node > admission-node | 到达 |
| admission-node > routing-node | 批准 |
| routing-node > step-node | 进入实例 |
| step-node > execution-node | 本步计划 | emphasis |

@steps id=path-tour
| input-tour | 官方输入 | 读取h/u、到达时间、T0和期限。 | execution-path | input-node |
| admission-tour | TFS准入 | 选择当前可派请求，先占用准入租约。 | execution-path | admission-node |
| routing-tour | Dynamo路由 | 真实SelectionService选择Worker并预留记账。 | execution-path | routing-node |
| step-tour | vLLM组step | 真实Scheduler决定本步成员和token数。 | execution-path | step-node |
| execution-tour | 逐层读算 | 模拟读取与计算，结束后更新真实调度器。 | execution-path | execution-node |""")
    add(
        "点击节点可查看源码。Dynamo路由是既有路径，本轮未修改 `router.py`。[](source:router)"
    )
    panel("代码改动｜范围和文件有哪些？", "code-scope")
    add(
        table(
            ["阶段", "提交范围", "工作"],
            [
                [
                    "初次TFS",
                    "`a870596 → 344f623`",
                    "准入、反馈、画像、旧循环时序与统计",
                ],
                ["SA对齐", "`344f623 → e6a0825`", "官方输入、同步Connector、逐层读算"],
                ["结果报告", "`e6a0825 → 8ec666f`", "只读归档生成HTML"],
                ["代码审查", "`607c7eb`", "原文、补丁与复核证据"],
            ],
        )
    )
    add(
        table(
            ["运行时文件", "状态", "用途"],
            [
                ["`sim/admission.py`", "新增", "排序、门槛、配额和租约"],
                ["`sim/request_profile.py`", "新增", "初次TFS线性画像和参数校验"],
                ["`sim/control_events.py`", "新增", "请求级反馈事件"],
                ["`sim/tfs_metrics.py`", "新增", "等待、分类和实际batch指标"],
                ["`sim/sa_aligned.py`", "新增", "SA事件循环与官方画像"],
                ["`sim/sa_layer_engine.py`", "新增", "同步Connector与层执行"],
                ["`sim/sa_model.py`", "新增", "SA画像和聚合层存储"],
                ["`sim/run.py`", "修改", "初次TFS准入、反馈和严格时序"],
                ["`sim/engine.py`", "修改", "事实日志与可选Connector名称"],
            ],
        )
    )
    add(
        "旧 `router.py`、`connector.py`、`storage.py`、版本固定文件和既有Ascend补丁原文未变。SA路径另建入口。[](source:audit)"
    )
    panel("代码改动｜输入和TFS准入怎样接入？", "code-admission")
    add(
        "导出器调用SA官方生成器。h/u原值不变；合成token ID只满足API。画像在请求到达后才交给控制器。[](source:input)[](source:arrivals)"
    )
    add(
        table(
            ["策略", "中央排序", "重流配额", "定位"],
            [
                ["immediate", "到达时间", "无", "DV直接提交基线"],
                ["slack_only", "剩余等待预算", "无", "本轮排序对照"],
                ["quota_fcfs", "到达时间", "有", "本轮配额对照"],
                ["tfs", "剩余等待预算", "有", "SA TFS请求级移植"],
            ],
        )
    )
    add("""两种配额策略都跳过暂不能批准的重请求，继续找轻请求。它们共用配额与释放，差别在排序。

@code source=admission language=python

剩余等待预算为 `deadline-now-T0`。所有候选共用同一个now，等待不会自动改变已有候选的相对预算次序。

重流压力为 `d=V_layer/c_single`。门槛取累计已到达请求历史的P75；完成请求仍在历史中。配额为 `max(1,floor(120GB/s/门槛))`。

SA路径对零门槛使用 `2^30` 配额，覆盖初次TFS的最小正值规则。正式实验关闭老化，反馈延迟为0。[](source:zero)

@code source=zero language=python""")
    panel("代码改动｜第3项改变了哪条接口？", "code-connector")
    add(
        "第3项改连接方式。上游Scheduler仍决定本步成员与token数。Connector先报告外部前缀h，执行器随后逐层等待KV。[](source:connector)[](source:schedule)"
    )
    add(
        table(
            ["行为", "旧异步整段路径", "新SA同步层路径"],
            [
                ["外部命中", "有KV时async=True", "精确h与async=False"],
                ["计算计划", "整段加载完成后恢复", "本步即可纳入计划"],
                ["数据等待", "整段KV完成再计算", "逐层等就绪再计算"],
                ["组批决策", "真实Scheduler", "真实Scheduler"],
            ],
        )
    )
    add("""@code source=connector language=python

@callout level=warning title="同步匹配不等于零耗时"
| `async=False` 允许先规划step。它不表示所有KV已读完。执行器仍阻止每层计算提前开始。 |""")
    diff = subprocess.check_output(
        [
            "git",
            "diff",
            "--unified=3",
            "344f623",
            "e6a0825",
            "--",
            "dynamo_vllm_cpu_sim/sim/engine.py",
        ],
        cwd=R,
        text=True,
    )
    add(
        '@diff caption="SA阶段在既有engine.py中只增加可选Connector名称"\n```diff\n'
        + diff.rstrip()
        + "\n```"
    )
    panel("代码改动｜第3、4项如何完成一个step？", "code-sequence")
    add(
        "顺序图按依赖展开一次step，后续层重复同一规则。图中位置不表示耗时。当前层计算可与下一层读取重叠。[](source:schedule)[](source:layers)"
    )
    add("""@sequence id=step-protocol title="真实调度计划与逐层模拟执行"
| actor executor | 仿真执行器 | backend | source:schedule |
| actor scheduler | vLLM调度器 | backend | source:schedule |
| actor connector | 同步Connector | backend | source:connector |
| actor storage | 聚合层存储 | database | source:storage |
| actor control | 准入控制器 | backend | source:lease |
| phase | 第3项：产生计算计划 |
| executor | scheduler | schedule() |
| scheduler | connector | 查询外部前缀 |
| connector | scheduler | h，async=False | return |
| scheduler | executor | 成员与token数 | return |
| phase | 第4项：读取、计算与反馈 |
| executor | storage | 提交第0层读取 |
| storage | executor | 第0层KV就绪 | return |
| executor | storage | 预取第1层KV |
| phase | 执行器算第0层，存储读第1层 |
| storage | executor | 第1层KV就绪 | return |
| phase | 后续层重复至第7层完成 |
| executor | scheduler | update_from_output |
| scheduler | executor | token与完成状态 | return |
| executor | control | 首token释放租约 | emphasis |

@steps id=protocol-tour
| plan | vLLM调度器 | 真实schedule决定本步成员，执行器不手工拼batch。 | step-protocol | scheduler |
| match | 同步Connector | 外部h进入本步规划，此时尚未执行模型计算。 | step-protocol | connector |
| read | 聚合层存储 | 存储竞争决定每层读取何时就绪。 | step-protocol | storage |
| run | 仿真执行器 | 等本层KV，计算当前层，并预取下一层。 | step-protocol | executor |
| release | 准入控制器 | 首token释放租约，让后续重请求进入。 | step-protocol | control |""")
    add("""执行器返回合成 `ModelRunnerOutput`。真实Scheduler更新状态并回收KV。主循环还更新Dynamo的prefill和预留记账。[](source:feedback)[](source:router)

@code source=feedback language=python""")
    panel("代码改动｜第4项如何算时间和分带宽？", "code-model")
    add(
        "真实step的token数进入SA画像。每批每层远端前缀合成一条读取流。[](source:model)[](source:storage)"
    )
    add("""@code source=model language=python""")
    add(
        table(
            ["规则", "实现含义"],
            [
                ["层计算", "`c=launch+aN/eta(N)+b·attention`，使用本步真实token"],
                ["首层读取", "申请q_max=200GB/s"],
                ["一层预取", "本层计算开始时提交下一层，申请min(q_max,V/c)"],
                ["到期升速", "本层结束仍未读完，申请升至q_max"],
                ["共享带宽", "事件点重算全部活跃层流的max_min分配"],
                ["欠载余量", "总申请低于容量时，余量均分；可超过申请率"],
                ["字节累计", "补偿求和减少大字节量累计舍入"],
            ],
        )
    )
    add(
        "旧逐盘/逐块Storage未修改。新LayerStorage只复用waterfill数学函数，不调用旧盘和Path队列。[](source:storage)"
    )
    add("""@code source=storage language=python

@code source=layers language=python""")
    panel("代码改动｜哪些行为与SA仍不同？", "code-differences")
    add(
        table(
            ["项目", "SA官方控制", "本轮DV", "影响"],
            [
                [
                    "派发",
                    "仅向空闲Worker派发",
                    "Dynamo可送入实例队列",
                    "上限1也非SA派发",
                ],
                ["批次", "E26f固定batch=1", "真实组批；上限1/8", "上限8，实际最大5"],
                [
                    "分块",
                    "singleton完整执行u",
                    "共享8192token预算，可分块",
                    "不止batch人数变化",
                ],
                ["配额占用", "在跑重批", "已批准且未释放的重请求", "包括实例内排队"],
                [
                    "唤醒",
                    "SA WAIT估计唤醒",
                    "到达、层读、层算事件",
                    "决策时刻不逐一相同",
                ],
                ["内存粒度", "逻辑预算", "block=1与真实KV管理", "非设备block=1证明"],
            ],
        )
    )
    add("不能把差异全部归因于组批。独立多请求batch端到端对拍尚未完成。[](source:audit)")
    panel("代码改动｜已有验证能证明什么？", "code-checks")
    add(
        table(
            ["2026-10-19证据", "结果", "证明范围"],
            [
                ["核心指纹", "12项上游、7项SA源匹配", "指定文件身份一致"],
                ["原始文件", "384个SHA匹配", "原report/events/config/check与清单一致"],
                ["请求重算", "34560条、16行汇总一致", "TTFT、期限与聚合一致"],
                ["轨迹重放", "1892688事件、34560次批准", "门槛、配额、排序与token推进"],
                ["实际step", "31085个；混合2540个", "来自真实step输出记录"],
                ["部分u步骤", "1761个请求-step", "步骤计数，不是独立请求数"],
                ["公式重算", "最大差1.39e-17秒", "独立Fraction公式，非硬件校准"],
                ["测试", "51项通过", "引用既有日志，不称今天新跑"],
                [
                    "独立SA对拍",
                    "8案例，最大差1.11e-16秒",
                    "均上限1；无多请求端到端对拍",
                ],
            ],
        )
    )
    add(
        "本次报告重新核对导出数据和主要指标。没有重跑完整矩阵或单元测试。[](source:audit)[](source:tests)[](source:parity)"
    )
    panel("代码改动｜完整208个文件清单", "code-manifest")
    add(
        "范围为 `a870596 → 8ec666f`。清单包含实验数据和图片。下面保留完整CSV原文与SHA256。[](source:inventory)"
    )
    inventory_groups = defaultdict(lambda: [0, 0])
    for row in I:
        inventory_groups[row["group"]][row["status"] == "修改"] += 1
    add(
        table(
            ["类别", "新增文件", "修改文件"],
            [[category, *counts] for category, counts in inventory_groups.items()],
        )
    )
    add("CSV可横向滚动，也可直接复制。若要先看实验，可使用上方跳转链接。")
    add("@code id=inventory-raw source=inventory language=csv")
    panel("实验报告｜本轮检验什么？", "experiment-start")
    add(
        "输入、资源、画像和存储对齐SA。实验考察TFS在真实路由、实例排队和自主组批下的表现。执行时间仍是仿真。[](source:original)"
    )
    add("""@facts
| 实验日期 | 2026-10-19 |
| SA源提交 | 3cd8269 |
| DV矩阵 | 4输入×2上限×4策略×3路由重复=96次 |
| SA控制 | 4输入×FCFS/TFS=8次 |
| 每次输入 | A/B/C各120，共360请求 |
| 输出 | 1token；首token即请求完成 |""")
    panel("实验报告｜输入负载为什么这样选？", "experiment-input")
    prof = json.loads((P / "configs/sa_e26f_profile.json").read_text())
    shapes = []
    for cl, h, u in [
        ("A", 42883, 256),
        ("B", 9399, 4096),
        ("C_A", 0, 2657),
        ("C_B", 0, 8110),
    ]:
        eta = max(F(prof["eta_floor"]), min(F(1), F(u, prof["N_sat"])))
        c = (
            F(prof["t_launch_s"])
            + F(prof["a_s_per_token"]) * u / eta
            + F(prof["b_s_per_pair"]) * (u * h + u * (u + 1) // 2)
        )
        v = F(prof["kappa_gb_per_token_layer"]) * 10**9 * h
        t = v / F(120 * 10**9)
        t0 = t + 8 * c + 7 * max(F(0), t - c)
        shapes.append(
            [
                cl,
                h,
                u,
                f"{float(v) / 1e6:.3f}",
                f"{float(c) * 1000:.3f}",
                f"{float(t0) * 1000:.3f}",
                f"{float(v / c) / 1e9:.3f}",
            ]
        )
    add(
        "为与SA直接对照，采用官方输入。h是外部前缀，u是待计算token。C_A/C_B替换同一位置的零读取C类。[](source:input)"
    )
    add(
        table(
            ["类", "h token", "u token", "层读取MB", "层计算ms", "T0 ms", "压力GB/s"],
            shapes,
        )
    )
    add(
        "C_A计算时间接近A；C_B接近B。A每簇16请求，簇心间隔 `Exp(16/98)`。B/C间隔 `Exp(1/98)`。各类名义速率98请求/秒。[](source:original)"
    )
    add(
        "C_A采用416/417/418；C_B采用424。每份360请求。期限为 `arrival+4T0`，不根据拥堵结果调整。[](source:input)"
    )
    panel("实验报告｜资源与存储算法", "experiment-resources")
    add(
        table(
            ["项目", "固定设置"],
            [
                ["实例与执行", "32Worker；8层；8192token/step"],
                ["序列上限", "max_num_seqs=1或8"],
                ["聚合存储", "120GB/s；读启动延迟0"],
                ["层读取", "q_max=200GB/s；4096bytes/token/layer"],
                ["分配", "SA max_min；每batch/layer一流；一层预取"],
                ["逻辑内存", "8GB；base0.5GB；KV32768bytes/token"],
                ["工作区", "131072bytes/计算token；预留8192token峰值"],
                ["缓存与输出", "前缀复用关；逻辑block=1；输出1token"],
                ["控制器", "历史P75；首token释放；老化关；反馈延迟0"],
                ["真实依赖", "Dynamo1.5.0；vLLM0.20.2；torch2.11 CPU"],
            ],
        )
    )
    add(
        "这里的max_min是聚合层流算法，不是旧DV逐盘逐块max_min_fair。资源模型相同不等于真实设备执行。[](source:original)"
    )
    panel("实验报告｜指标和聚合口径", "experiment-metrics")
    add(
        "TTFT（首token延迟）从到达计时，包含中央和实例内等待。百分比是请求期限达标率，不是资源利用率。[](source:original)"
    )
    add("""```text
TTFT = first_token_s - arrival_s
达标率 = 满足 first_token_s <= deadline_s 的请求数 / 360
每种子指标 = 同输入3次路由重复中位数
最终指标 = 官方输入种子指标中位数
```""")
    add(
        "C_A有3个输入种子，C_B只有1个。路由重复不能增加输入种子数。各指标独立取中位数，分类中位数不能加权还原总指标。[](source:summary)[](source:classes)"
    )
    panel("实验报告｜TFS是否改善直接提交？", "experiment-primary")
    add(
        '@chart type=bar data=attainment.csv x=case y=immediate,slack_only,quota_fcfs,tfs unit="%" title="请求期限达标率：同工况比较四策略"'
    )
    gains = []
    for v in "AB":
        for cap in ("1", "8"):
            a = next(
                r
                for r in S
                if r["variant"] == v and r["cap"] == cap and r["mode"] == "immediate"
            )
            t = next(
                r
                for r in S
                if r["variant"] == v and r["cap"] == cap and r["mode"] == "tfs"
            )
            gains.append(
                [
                    f"C_{v}",
                    cap,
                    pct(a["slo"]),
                    pct(t["slo"]),
                    f"+{(float(t['slo']) - float(a['slo'])) * 100:.2f}个百分点",
                ]
            )
    add(table(["工况", "上限", "直接提交", "TFS", "差值"], gains))
    add(
        "差值用未舍入比例相减。C_A上限1为 `(0.700000−0.402778)×100=29.72` 个百分点。这不是相对增长百分比。[](source:summary)"
    )
    panel("实验报告｜全部16组策略结果", "experiment-all")
    add(
        table(
            [
                "工况",
                "上限",
                "策略",
                "达标率",
                "TTFT ms",
                "中央等待ms",
                "实际batch均值",
            ],
            [
                [
                    f"C_{r['variant']}",
                    r["cap"],
                    r["mode"],
                    pct(r["slo"]),
                    n(r["ttft_ms"]),
                    n(r["central_wait_ms"]),
                    n(r["batch_mean"]),
                ]
                for r in S
            ],
        )
    )
    add(
        "C_B上限1的quota_fcfs平均TTFT更低，两者达标率相同。不能称TFS始终优于quota_fcfs。[](source:summary)"
    )
    add(
        '@chart type=bar data=ttft.csv x=case y=immediate,slack_only,quota_fcfs,tfs unit=ms title="平均首token延迟：包含中央等待"'
    )
    panel("实验报告｜逐种子也复现SA了吗？", "experiment-parity")
    comp = []
    for v, seeds in [("A", [416, 417, 418]), ("B", [424])]:
        for seed in seeds:
            sa = lambda mode: next(
                r
                for r in SA
                if r["variant"] == v and int(r["seed"]) == seed and r["mode"] == mode
            )
            dv = lambda cap, mode: next(
                r
                for r in B
                if r["variant"] == v
                and int(r["seed"]) == seed
                and int(r["cap"]) == cap
                and r["mode"] == mode
            )
            comp.append(
                [
                    f"C_{v}/{seed}",
                    pct(sa("fcfs")["slo"]),
                    pct(sa("tfs")["slo"]),
                    pct(dv(1, "immediate")["slo"]),
                    pct(dv(1, "tfs")["slo"]),
                    pct(dv(8, "immediate")["slo"]),
                    pct(dv(8, "tfs")["slo"]),
                ]
            )
    add(
        table(
            [
                "工况/seed",
                "SA FCFS",
                "SA TFS",
                "DV直接1",
                "DV TFS1",
                "DV直接8",
                "DV TFS8",
            ],
            comp,
        )
    )
    add("""@callout level=warning title="seed418是明确反例"
| C_A/418：SA TFS为71.1%；DV上限1为64.2%，上限8为57.5%。该种子未复现SA达标率。 |""")
    add(
        "SA FCFS只向空闲实例派发，DV直接提交会进入实例队列。两者不是相同排队基线。[](source:sa)[](source:seed)"
    )
    panel("实验报告｜全部48行分类结果", "experiment-classes")
    cg = defaultdict(list)
    for r in C:
        cg[tuple(r[k] for k in ("variant", "seed", "cap", "mode", "class"))].append(r)
    cm = defaultdict(list)
    for (v, s, c, m, cl), rs in cg.items():
        cm[(v, c, m, cl)].append(
            {
                k: statistics.median(float(r[k]) for r in rs)
                for k in ("slo", "ttft_ms", "ttft_p95_ms", "central_wait_ms")
            }
        )
    cr = []
    for (v, c, m, cl), rs in sorted(cm.items()):
        x = {
            k: statistics.median(r[k] for r in rs)
            for k in ("slo", "ttft_ms", "ttft_p95_ms", "central_wait_ms")
        }
        cr.append(
            [
                f"C_{v}",
                c,
                m,
                cl,
                pct(x["slo"]),
                n(x["ttft_ms"]),
                n(x["ttft_p95_ms"]),
                n(x["central_wait_ms"]),
            ]
        )
    assert len(cr) == 48
    add("C代表当前工况的零读类。下表各指标分别按双层中位数计算。[](source:classes)")
    add(
        table(
            ["工况", "上限", "策略", "类", "达标率", "TTFT ms", "P95 ms", "中央等待ms"],
            cr,
        )
    )
    add(
        "TFS下A类仍可能大量超时。A类达标率仍低。混合step中的C也可能等待同批远端KV。[](source:original)"
    )
    panel("实验报告｜计算、中央队列与带宽", "experiment-traces")
    add(
        "固定展示seed416/424、上限8、repeat0。蓝=A，橙=B，绿=C，紫=混合batch。甘特只绘计算，空白含等待和空闲。带宽为10ms字节积分均值。[](source:original)"
    )
    add(
        '@image path=assets/sa_alignment_A_timeline.png alt="C_A计算甘特、队列和带宽" caption="C_A：seed416，上限8，repeat0；归档仿真"'
    )
    add(
        '@image path=assets/sa_alignment_B_timeline.png alt="C_B计算甘特、队列和带宽" caption="C_B：seed424，上限8，repeat0；归档仿真"'
    )
    add(
        "实际batch最大5，配置上限8，实际每步最多5请求。混合step会引入层屏障影响，但这项作用没有单独消融。[](source:original)"
    )
    panel("实验报告｜全部96次运行与波动", "experiment-runs")
    add(
        '@image path=assets/sa_alignment_ttft_spread.png alt="全部路由重复的平均TTFT分布" caption="每点是一轮平均TTFT；路由重复不等于独立负载"'
    )
    add("repeat是同输入的路由重复编号。原生同代价平局可能产生不同落点。[](source:runs)")
    add(
        table(
            [
                "工况",
                "seed",
                "上限",
                "策略",
                "repeat",
                "达标率",
                "TTFT ms",
                "中央等待ms",
                "batch最大",
            ],
            [
                [
                    f"C_{r['variant']}",
                    r["seed"],
                    r["cap"],
                    r["mode"],
                    r["repeat"],
                    pct(r["slo"]),
                    n(r["ttft_ms"]),
                    n(r["central_wait_ms"]),
                    r["batch_max"],
                ]
                for r in U
            ],
        )
    )
    panel("实验报告｜守恒和资源检查", "experiment-validation")
    add(
        table(
            ["检查", "归档结果", "限制"],
            [
                ["读取量", "每次205581189120bytes；累计误差0", "非设备吞吐测量"],
                ["内存", "逻辑峰值5.370078464GB，小于8GB", "非物理显存执行"],
                ["KV空闲", "最小64039slots；最大请求43140slots", "容量未成为主要限制"],
                [
                    "生命周期",
                    "无抢占或重复加载；KV和路由归零",
                    "不覆盖故障、取消、写回",
                ],
                ["对拍", "8案例；最大差1.11e-16秒", "均上限1，无多请求端到端对拍"],
                ["核心源码", "12项保护指纹一致", "非全依赖环境认证"],
            ],
        )
    )
    add(
        "开发时出现约0.01byte累计舍入漂移。改用补偿求和后统一实现并重跑正式矩阵。没有调整物理速率来凑结果。[](source:original)"
    )
    panel("实验报告｜哪些结论不能下？", "experiment-limits")
    add(
        table(
            ["判断", "支持范围", "原因"],
            [
                ["TFS改善DV直接提交", "限本轮工况", "逐请求与汇总一致"],
                ["TFS始终优于quota_fcfs", "不支持", "部分指标相同或quota_fcfs更好"],
                ["组批必然提高TFS达标率", "不支持", "上限8汇总未超过上限1"],
                ["逐场景复现SA", "不支持", "seed418差距明显"],
                ["差异唯一来自组批人数", "不支持", "路由、排队、占用与分块也变化"],
                ["生产设备具有同样收益", "不支持", "无真实数值执行或设备校准"],
                ["结果均统计显著", "不支持", "C_B单种子；无置信区间和显著性检验"],
            ],
        )
    )
    add(
        "后续需独立多请求batch参考执行器，以及路由、排队、配额占用、组批的控制变量消融。本轮未完成这些实验。[](source:audit)"
    )
    panel("实验报告｜如何复核？", "experiment-reproduce")
    add(
        "源码摘录固定到本地提交607c7eb，并内嵌于页面。远端暂无写权限，公共链接可能无法访问该提交。页面内的图表和摘录可离线阅读。"
    )
    add("""```bash title="在dynamo_vllm_cpu_sim目录复核"
.venv/bin/python scripts/audit_sa_code_review.py
.venv/bin/python -m unittest discover -s tests -v
# 审查依赖本机完整results/sa_alignment_20261019。
# 测试应以新运行日志为准，不把旧记录当作新结果。
```""")
    add(
        "精简归档 `results/sa_alignment_summary_20261019` 含官方输入、SA控制、逐次/种子/分类/请求CSV、对拍和原始SHA。完整事件保留在本机。[](source:original)"
    )
    add("""- [ ] 检查节点源码，确认真实调度和模拟执行边界。
- [ ] 在同工况、同上限内比较策略。
- [ ] 查看seed418，不只看汇总中位数。
- [ ] 区分请求达标率和资源利用率。
- [ ] 检查组批归因尚缺的消融证据。""")
    draft = "\n\n".join(out) + "\n"
    (d / "candidate.archify.md").write_text(draft)
    evidence = {
        "source_revision": REV,
        "archive_date": "2026-10-19",
        "report_date": "2026-10-20",
        "new_simulation_runs": 0,
        "recomputed_runs": 96,
        "recomputed_request_rows": 34560,
        "summary_rows": 16,
        "class_summary_rows": 48,
        "run_rows": 96,
        "changed_files": 208,
        "draft_sha256": hashlib.sha256(draft.encode()).hexdigest(),
        "input_hashes": {
            str(p.relative_to(R)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                A / "summary.csv",
                A / "runs.csv",
                A / "requests.csv",
                A / "classes.csv",
                V / "audit.json",
            ]
        },
    }
    (d / "build-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "folder": str(d),
                "panels": draft.count("\n## "),
                "bytes": len(draft.encode()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
