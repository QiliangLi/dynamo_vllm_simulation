#!/usr/bin/env python3
"""只读固定 Git 差异与审查证据，生成自包含代码改动说明。"""

import base64
from collections import Counter
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "dynamo_vllm_cpu_sim"
EVIDENCE = PKG / "results/sa_code_review_20261019"
BASE, TFS, SA, HEAD = "a870596", "344f623", "e6a0825", "8ec666f"
RANGES = {"all": (BASE, HEAD), "tfs": (BASE, TFS), "sa": (TFS, SA), "html": (SA, HEAD)}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def esc(value):
    return html.escape(str(value), quote=True)


def pre_escape(value):
    """保留补丁尾空白的显示原文，同时避免 HTML 源码尾空白。"""
    return re.sub(
        r"[ \t]+(?=\n|$)",
        lambda m: "".join("&#32;" if c == " " else "&#9;" for c in m[0]),
        esc(value),
    )


def table(headers, rows):
    return (
        "<table><thead><tr>"
        + "".join(f"<th>{esc(h)}</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>" for row in rows
        )
        + "</tbody></table>"
    )


def group(path):
    if "/results/" in path:
        return "实验数据与日志"
    if "docs/figures/" in path:
        return "实验图片"
    if "/sim/" in path:
        return "运行时"
    if "/tests/" in path:
        return "测试"
    if "/configs/" in path:
        return "配置"
    if "/scripts/" in path:
        return "工具与模板"
    return "文档与仓库配置"


def stats(before, after):
    records = []
    for item in git("diff", "--numstat", "-z", before, after).split(b"\0"):
        if not item:
            continue
        added, removed, name = item.decode().split("\t", 2)
        path = name
        data = git("show", f"{after}:{path}")
        exists = (
            subprocess.run(
                ["git", "cat-file", "-e", f"{before}:{path}"],
                cwd=ROOT,
                capture_output=True,
            ).returncode
            == 0
        )
        records.append(
            {
                "path": path,
                "group": group(path),
                "status": "修改" if exists else "新增",
                "added": added,
                "removed": removed,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return records


def ref(file, symbol, label="查看代码"):
    return f'<button class="ref" data-file="{esc(file)}" data-symbol="{esc(symbol)}">{esc(label)}</button>'


def modules():
    rows = [
        (
            "中央准入控制",
            "sim/admission.py",
            "class AdmissionController",
            "原底座在请求到达后直接路由，没有中央扣留队列。",
            "新增四种策略：immediate、slack_only、quota_fcfs、tfs。画像来自已到达请求。按 P75 计算重流门槛；批准时先原子占用租约，首 token 或指定反馈到达时释放。",
            "改变何时进入 Dynamo；不替代 Dynamo 的 Worker 选择，也不直接指定 vLLM batch。",
            "本次按原始输入重放全部 34,560 次批准，核对候选次序、配额与释放。SA 路径关闭老化。",
        ),
        (
            "冻结画像与反馈事件",
            "sim/request_profile.py",
            "def build_profile",
            "原底座没有 TFS 请求画像或控制反馈账本。",
            "初次 TFS 新增线性参考画像、输入校验和 ControlEvent。普通 sim.run 区分事件发生与可见时间，支持加载完成/首 token 双释放口径。",
            "初次画像不是 SA 画像；SA 路径从官方 h/u 和独占 T0 构造 RequestProfile，直接复用账本结构。",
            "测试覆盖分位下标、零值、延迟反馈和幂等释放；正式 SA 延迟=0、first_token 释放。",
        ),
        (
            "旧事件循环",
            "sim/run.py",
            "async def simulate",
            "到达后直接逐请求路由；整段外部 KV 收齐后再恢复真实 Scheduler。",
            "接入中央队列、反馈优先队列和准入唤醒；增加逐类/逐请求/batch 记录。到达和完成时刻使用严格比较，移除原 <= now+1e-12 的提前容差。反馈排空不延长业务时间。",
            "初次 TFS 改动范围较大，除新策略还包含时序边界修复与统计扩展；不能称为只新增一个独立函数。",
            "既有七模式回归已归档；当前 51 项测试包含 math.nextafter 邻接浮点到达边界。SA 正式矩阵另走 sim.sa_aligned。",
        ),
        (
            "真实 Scheduler 的现有桥接层",
            "sim/engine.py",
            "class Engine",
            "创建真实 Scheduler、构造虚拟模型输出；Connector 名称固定 SimConnector。",
            "初次接入仅增加请求级事实与真实 step 日志。SA 阶段在该文件只改一行：Connector 名称可由配置选择，默认仍为 SimConnector。",
            "没有改写上游 Scheduler 或 Allocator。模型输出仍由仿真构造；新增日志不是数值推理。",
            "原始差异可分别看两个阶段；上游 12 个指定保护文件指纹均匹配。",
        ),
        (
            "SA 对齐主循环",
            "sim/sa_aligned.py",
            "async def simulate",
            "原底座与初次 TFS 没有 SA 专用执行入口。",
            "新增独立入口，校验 h/u、T0、arrival+4T0 期限；以到达/层读/层算事件推进。批准后调用现有 Router，再交给真实 Scheduler。SAAdmission 对零 P75 使用 0 门槛与 2³⁰ 配额。",
            "保留真实路由与 Worker 队列。控制器按请求占用配额，包含已批准但还在 Worker 排队的请求。",
            "当前 sim/*.py 指纹与正式 96 次运行一致；逐请求字段和准入次序均重新核对。",
        ),
        (
            "同步层 Connector 与真实组批",
            "sim/sa_layer_engine.py",
            "class SALayerConnector",
            "原 SimConnector 使用异步整段加载，Scheduler 等 finished_recving 后恢复。",
            "新增 SALayerConnector，返回精确外部 h 和 async=False。真实 scheduler.schedule() 决定本步成员及 token 数；SALayerEngine 接管该 step 的 8 层读算模拟，最后交回真实 update_from_output。",
            "加载机制确实改变。当前 step 可以在执行中逐层等 KV；这是自定义仿真桥接，不是原异步 Connector 的原样运行。",
            "每层计算前必须读就绪；每请求累计真实 scheduled token 必须等于原 u。独立重放发现分块步骤，证明上限 8 并非只改变 batch 人数。",
        ),
        (
            "逐层计算与存储模型",
            "sim/sa_model.py",
            "class LayerStorage",
            "旧 Storage 按块、盘与 Path 分配；旧计算为线性 token 系数。",
            "新增每 batch/layer 聚合流。首层申请 q_max，预取层申请 min(q_max,V/c)，到期未读完升为 q_max。事件点重新 max_min 分配；c=t_launch+aN/eta+bA 使用 SA 原系数。",
            "这是执行模型替换，目的是与 SA 同源。旧 Storage 执行器未修改；只复用原 waterfill 数学函数。补偿求和减少大字节累计漂移。",
            "8 个既有 cap1 独立 SA 引擎对拍；当前另用 Fraction 重算所有记录 step 的画像。多请求端到端独立对拍仍缺。",
        ),
        (
            "指标、汇总与图",
            "sim/tfs_metrics.py",
            "def summarize",
            "原底座只给少量全局指标，难以区分中央等待和引擎等待。",
            "新增 TTFT 分解、期限达标率、逐类指标和真实 SchedulerOutput batch 统计。分析脚本先取每输入种子的 3 次路由重复中位数，再取输入种子中位数。",
            "期限达标率分母是请求数；不是 NPU 或带宽利用率。图只固定选 seed416/424、repeat0 作时序例子。",
            "本次用全部原请求完成时间重算，汇总一致；没有把路由重复当作独立输入种子。",
        ),
    ]
    out = []
    for title, file, symbol, before, after, impact, evidence in rows:
        out.append(
            f'<article class="module"><h3>{esc(title)} <code>{esc(file)}</code></h3><div class="compare"><div><div class="label">修改前</div><p>{esc(before)}</p></div><div><div class="label">修改后</div><p>{esc(after)}</p></div></div><p><strong>对行为的影响：</strong>{esc(impact)}</p><p class="evidence">{esc(evidence)} {ref(file, symbol)}</p></article>'
        )
    return "".join(out)


def main():
    audit = json.loads((EVIDENCE / "audit.json").read_text())
    testlog = (EVIDENCE / "unit-tests.log").read_text()
    if "Ran 51 tests" not in testlog or not testlog.rstrip().endswith("OK"):
        raise ValueError("fresh unit-test evidence required")
    inventory = stats(BASE, HEAD)
    groups = Counter(row["group"] for row in inventory)
    runtime = [r for r in inventory if r["group"] == "运行时"]
    assert len(runtime) == 9 and sum(r["status"] == "新增" for r in runtime) == 7
    files = []
    paths = [
        r["path"]
        for r in inventory
        if r["group"] in ("运行时", "配置", "工具与模板", "测试")
    ]
    for path in paths:
        stages = {}
        for stage, (before, after) in RANGES.items():
            diff = git(
                "diff", "--no-ext-diff", "--unified=4", before, after, "--", path
            ).decode()
            if not diff:
                continue
            old = subprocess.run(
                ["git", "show", f"{before}:{path}"], cwd=ROOT, capture_output=True
            )
            new = subprocess.run(
                ["git", "show", f"{after}:{path}"], cwd=ROOT, capture_output=True
            )
            stages[stage] = {
                "range": f"{before} → {after}",
                "before": old.stdout.decode() if old.returncode == 0 else None,
                "after": new.stdout.decode() if new.returncode == 0 else None,
                "diff": diff,
            }
        files.append({"path": path, "stages": stages})
    patch = git("diff", "--no-ext-diff", "--unified=4", BASE, HEAD, "--", *paths)
    flows = [
        (
            "已到达请求",
            "官方输入 → 冻结画像",
            "输入来自 SA 官方生成器；保留 h/u、到达时间、T0 与期限。合成 token ID 仅满足真实 API，不代替模型内容。",
            "sim/sa_aligned.py",
            "def materialize",
        ),
        (
            "请求级准入",
            "自写控制器",
            "控制器只决定哪个请求现在进入路由。先占用租约，再派发；尚在 Worker 队列的重请求也计入配额。",
            "sim/admission.py",
            "def reserve",
        ),
        (
            "Dynamo 选 Worker",
            "真实 SelectionService",
            "调用原有 select_and_reserve 和原生记账。路由仍为 cache-blind。此文件相对基线未改，完整流程说明不把它伪称为新增代码。",
            "",
            "",
        ),
        (
            "vLLM 组 step",
            "真实 Scheduler / KVManager",
            "真实 schedule() 决定请求集合和每请求 token 数。SALayerConnector 返回同步外部命中，使该 step 的层读取在执行器中等待。",
            "sim/sa_layer_engine.py",
            "def schedule_layer_step",
        ),
        (
            "读算与反馈",
            "合成层时长、聚合存储",
            "自写 LayerStorage 和 SALayerEngine 推进 8 层，随后将合成 ModelRunnerOutput 交回真实调度器。首 token 释放准入租约并更新 Dynamo 记账。",
            "sim/sa_layer_engine.py",
            "def close_layers",
        ),
    ]
    flowdata = []
    for title, subtitle, text, file, symbol in flows:
        body = f"<h3>{esc(title)} · {esc(subtitle)}</h3><p>{esc(text)}</p>"
        if file:
            body += ref(file, symbol)
        else:
            body += '<p class="evidence">sim/router.py 与 a870596 字节一致；源路径与上游指纹见审查记录。</p>'
        flowdata.append({"html": body})
    buttons = "".join(
        f'<button type="button" data-flow="{i}" aria-pressed="{str(i == 0).lower()}"><b>{esc(t)}</b><small>{esc(s)}</small></button>'
        for i, (t, s, *_) in enumerate(flows)
    )
    toolrows = [
        (
            "scripts/export_sa_e26f.py",
            "读取固定 SA 源，直接导出官方负载；在独立进程中运行 8 次 SA FCFS/TFS 控制，保存独立对拍真值。",
        ),
        (
            "scripts/run_sa_alignment.py",
            "4 份输入 × 2 个序列上限 × 4 个策略 × 3 次路由重复=96 次；冻结配置与实现 SHA，逐格运行/检查。",
        ),
        (
            "scripts/check_sa_results.py / verify_sa_alignment.py",
            "前者检查每次运行的因果与守恒；后者把 8 个最小实例与 SA 独立引擎真值比较。",
        ),
        (
            "scripts/analyze_sa_alignment.py",
            "读取已验收数据，生成 runs/per_seed/summary/classes/requests CSV；保存固定示例和原始哈希，画图。",
        ),
        (
            "scripts/gen_tfs_trace.py / run_tfs_matrix.py / analyze_tfs.py",
            "初次 TFS 的自建负载与矩阵工具。正式 SA 对齐输入不由 gen_tfs_trace 生成。",
        ),
        (
            "configs/sa_e26f.json / sa_e26f_profile.json",
            "固定 SA 系数、资源、合成内存和 SAAdmission 参数；上限 8 为批处理扩展对照，上限 1 为 singleton 限制对照。",
        ),
        (
            "tests/test_admission* / test_request_profile / test_sa_model / test_tfs_tools",
            "新增准入、画像、工具和 SA 模型检查。原有测试与当前新增测试合计 51 项。",
        ),
        (
            "scripts/build_sa_report_html.py / templates/sa_report.html",
            "上一份 HTML 结果报告的展示工具；只读归档，不进入 sim/*.py，也不改变实验数值。",
        ),
    ]
    stages = []
    for key, title, text in [
        ("tfs", "接入请求级 TFS", "新增中央扣留、租约、反馈和统计；修改旧事件循环。"),
        (
            "sa",
            "对齐 SA 输入与执行模型",
            "新增逐层读算入口；切换同步外部前缀桥接；运行正式 96 次矩阵。",
        ),
        ("html", "生成结果报告", "展示与下载已有数据；仿真运行时未变。"),
    ]:
        a, b = RANGES[key]
        count = len(stats(a, b))
        stages.append(
            f'<article class="stage"><h3>{esc(title)}</h3><span class="pill">{a} → {b} · {count} 文件</span><p>{esc(text)}</p></article>'
        )
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(inventory[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(inventory)
    manifest_bytes = stream.getvalue().encode()
    downloads = []
    for name, content, mime in [
        ("tfs-sa-code.patch", patch, "text/plain"),
        ("changed-files.csv", manifest_bytes, "text/csv"),
        (
            "code-review-audit.json",
            (EVIDENCE / "audit.json").read_bytes(),
            "application/json",
        ),
        (
            "audit_sa_code_review.py",
            (PKG / "scripts/audit_sa_code_review.py").read_bytes(),
            "text/plain",
        ),
        ("unit-tests.log", testlog.encode(), "text/plain"),
    ]:
        downloads.append(
            f'<a download="{name}" href="data:{mime};base64,{base64.b64encode(content).decode()}">{esc(name)}</a>'
        )
    invhtml = "".join(
        f'<tr data-group="{esc(r["group"])}" data-path="{esc(r["path"].lower())}"><td>{esc(r["path"])}</td><td>{esc(r["group"])}</td><td>{r["status"]}</td><td class="count">{r["added"]}</td><td class="count">{r["removed"]}</td><td class="hash">{r["sha256"]}</td></tr>'
        for r in inventory
    )
    protected = table(
        ["保护文件", "本次 SHA256"],
        [
            [
                f"<code>{esc(r['file'])}</code>",
                f'<span class="hash">{r["sha256"]}</span>',
            ]
            for r in audit["protected_sources"]
        ],
    )
    data = {"files": files, "flows": flowdata, "inventoryCount": len(inventory)}
    checks = audit["checks"]
    payload = {
        "EVENT_COUNT": f"{checks['independent_event_replay']['events']:,}",
        "CHUNK_COUNT": f"{checks['independent_event_replay']['chunked_request_steps']:,}",
        "STAGES": "".join(stages),
        "GROUP_TABLE": table(
            ["类别", "文件数", "新增 / 修改"],
            [
                [
                    esc(k),
                    str(n),
                    str(
                        sum(r["status"] == "新增" for r in inventory if r["group"] == k)
                    )
                    + " / "
                    + str(
                        sum(r["status"] == "修改" for r in inventory if r["group"] == k)
                    ),
                ]
                for k, n in groups.items()
            ],
        ),
        "FILE_COUNT": str(len(inventory)),
        "FLOW_BUTTONS": buttons,
        "FLOW_INITIAL": flowdata[0]["html"],
        "FLOW_STATIC": "".join(
            f'<article class="module">{x["html"]}</article>' for x in flowdata
        ),
        "MODULES": modules(),
        "TOOLS": table(
            ["位置", "用途与边界"],
            [[f"<code>{esc(f)}</code>", esc(t)] for f, t in toolrows],
        ),
        "TEST_TIME": str(checks["fresh_unit_tests"]["wall_s"]),
        "LAYER_ERROR": f"{checks['independent_event_replay']['max_layer_compute_error_s']:.3g}",
        "STEP_COUNT": f"{checks['independent_event_replay']['actual_steps']:,}",
        "MIXED_COUNT": f"{checks['independent_event_replay']['mixed_steps']:,}",
        "AUDIT_PRE": esc(json.dumps(audit, ensure_ascii=False, indent=2)),
        "FULL_PATCH": pre_escape(patch.decode()),
        "DOWNLOADS": "".join(downloads),
        "GROUP_OPTIONS": "".join(
            f'<option value="{esc(g)}">{esc(g)}</option>' for g in groups
        ),
        "INVENTORY": invhtml,
        "PROTECTED": protected,
        "IMPL_SHA": audit["implementation_sha256"],
        "DATA": json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"),
    }
    text = (PKG / "scripts/templates/sa_code_review.html").read_text()
    # Replace placeholders once: embedded Git patches may themselves contain @@...
    text = re.sub(r"@@([A-Z_]+)@@", lambda m: payload[m[1]], text)
    target = ROOT / "TFS与SA对齐代码改动说明-20261019.html"
    target.write_text(text)
    (EVIDENCE / "changed-files.csv").write_bytes(manifest_bytes)
    (EVIDENCE / "tfs-sa-code.patch").write_bytes(patch)
    print(
        json.dumps(
            {
                "output": str(target),
                "bytes": target.stat().st_size,
                "all_changed_files": len(inventory),
                "source_files": len(files),
                "runtime_files": len(runtime),
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
