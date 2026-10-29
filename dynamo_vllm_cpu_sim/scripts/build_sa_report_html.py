#!/usr/bin/env python3
"""从已验收的 SA 对齐归档生成自包含 HTML；不重跑或修改任何实验数据。"""

import argparse
import base64
from collections import defaultdict
import csv
from fractions import Fraction
import gzip
from html import escape
import hashlib
from io import BytesIO
import json
from pathlib import Path
import statistics
import zipfile

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parent
MODES = ("immediate", "slack_only", "quota_fcfs", "tfs")
LABELS = {
    "immediate": "直接提交",
    "slack_only": "仅等待预算排序",
    "quota_fcfs": "配额 + 到达顺序",
    "tfs": "TFS：配额 + 等待预算排序",
}


def csv_rows(path):
    rows = list(csv.DictReader(path.open()))
    for row in rows:
        for key, value in row.items():
            if key in (
                "variant",
                "mode",
                "class",
                "id",
                "class_id",
                "cell",
                "file",
                "sha256",
            ):
                continue
            try:
                row[key] = float(value)
            except (ValueError, TypeError):
                pass
    return rows


def aggregate(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row["seed"]].append(row[key])
    return statistics.median(statistics.median(v) for v in groups.values())


def table(headers, rows, identifier="", highlight=None):
    result = [
        f'<div class="table-scroll" tabindex="0" aria-label="{escape(identifier or headers[0])}，可横向滚动"><table id="{escape(identifier)}"><thead><tr>'
    ]
    result += [f'<th scope="col">{escape(h)}</th>' for h in headers]
    result += ["</tr></thead><tbody>"]
    for i, row in enumerate(rows):
        result.append(
            "<tr"
            + (' class="exception-row"' if highlight and highlight(i, row) else "")
            + ">"
        )
        result += [
            f"<{'th scope="row"' if j == 0 else 'td'}>{escape(str(v))}</{'th' if j == 0 else 'td'}>"
            for j, v in enumerate(row)
        ]
        result.append("</tr>")
    return "".join(result) + "</tbody></table></div>"


def download(name, label, data, mime):
    return f'<a class="download" href="data:{mime};base64,{base64.b64encode(data).decode()}" download="{escape(name)}">{escape(label)}</a>'


def build(archive, figures, output):
    summary = json.loads((archive / "summary.json").read_text())
    audit = json.loads((archive / "audit.json").read_text())
    source = json.loads((archive / "source/source.json").read_text())
    runtime = json.loads((archive / "runtime-provenance.json").read_text())
    runs, classes = csv_rows(archive / "runs.csv"), csv_rows(archive / "classes.csv")
    sa, per_seed = (
        csv_rows(archive / "sa_controls.csv"),
        csv_rows(archive / "per_seed.csv"),
    )
    assert len(runs) == 96 and audit["passed"] and len(sa) == 8
    assert all(
        v["passed"] for v in json.loads((archive / "verification.json").read_text())
    )
    for record in summary["summary"]:
        selected = [
            r
            for r in runs
            if r["variant"] == record["variant"]
            and r["cap"] == record["cap"]
            and r["mode"] == record["mode"]
        ]
        for key in ("slo", "ttft_ms", "central_wait_ms", "batch_mean"):
            assert abs(aggregate(selected, key) - record[key]) < 1e-10
    data = {
        "runs": runs,
        "classes": classes,
        "sa": sa,
        "labels": LABELS,
        "modes": list(MODES),
    }
    find = lambda v, c, m: next(
        r
        for r in summary["summary"]
        if r["variant"] == v and r["cap"] == c and r["mode"] == m
    )
    pct = lambda x: f"{100 * x:.1f}%"
    core = []
    for v in "AB":
        sv = lambda mode: statistics.median(
            r["slo"] for r in sa if r["variant"] == v and r["mode"] == mode
        )
        core.append(
            [f"C_{v}", pct(sv("fcfs")), pct(sv("tfs"))]
            + [pct(find(v, c, m)["slo"]) for c in (1, 8) for m in ("immediate", "tfs")]
        )
    seed_rows = []
    for cell in source["cells"]:
        v, seed = cell["variant"], cell["seed"]
        sv = lambda mode: next(
            r["slo"]
            for r in sa
            if r["variant"] == v and r["seed"] == seed and r["mode"] == mode
        )
        dv = lambda cap, mode: next(
            r["slo"]
            for r in per_seed
            if r["variant"] == v
            and r["seed"] == seed
            and r["cap"] == cap
            and r["mode"] == mode
        )
        seed_rows.append(
            [f"C_{v} / {seed}", pct(sv("fcfs")), pct(sv("tfs"))]
            + [pct(dv(c, m)) for c in (1, 8) for m in ("immediate", "tfs")]
        )
    all_rows = []
    for v in "AB":
        for cap in (1, 8):
            for mode in MODES:
                r = find(v, cap, mode)
                all_rows.append(
                    [
                        f"C_{v}",
                        cap,
                        mode,
                        pct(r["slo"]),
                        f"{r['ttft_ms']:.2f}",
                        f"{r['central_wait_ms']:.2f}",
                        f"{r['batch_mean']:.2f}",
                        f"{r['makespan_s']:.3f}",
                    ]
                )
    class_rows = []
    for v in "AB":
        for cap in (1, 8):
            for mode in MODES:
                for cls in "ABC":
                    rs = [
                        r
                        for r in classes
                        if r["variant"] == v
                        and r["cap"] == cap
                        and r["mode"] == mode
                        and r["class"] == cls
                    ]
                    class_rows.append(
                        [
                            f"C_{v}",
                            cap,
                            mode,
                            cls,
                            pct(aggregate(rs, "slo")),
                            f"{aggregate(rs, 'ttft_ms'):.2f}",
                            f"{aggregate(rs, 'ttft_p95_ms'):.2f}",
                        ]
                    )
    run_rows = [
        [
            f"C_{r['variant']} / {int(r['seed'])}",
            int(r["cap"]),
            r["mode"],
            int(r["repeat"]),
            pct(r["slo"]),
            f"{r['ttft_ms']:.2f}",
            f"{r['central_wait_ms']:.2f}",
            f"{r['batch_mean']:.2f}",
            int(r["batch_max"]),
        ]
        for r in runs
    ]
    profile = source["profile"]
    shapes = []
    for name, h, u in (
        ("A", 42883, 256),
        ("B", 9399, 4096),
        ("C_A", 0, 2657),
        ("C_B", 0, 8110),
    ):
        eta = min(1, max(Fraction(profile["eta_floor"]), Fraction(u, profile["N_sat"])))
        c = (
            Fraction(profile["t_launch_s"])
            + Fraction(profile["a_s_per_token"]) * u / eta
            + Fraction(profile["b_s_per_pair"]) * (u * h + u * (u + 1) // 2)
        )
        v = Fraction(profile["kappa_gb_per_token_layer"]) * h
        t0 = v / 120 + 8 * c + 7 * max(Fraction(0), v / 120 - c)
        shapes.append(
            [
                name,
                f"{h:,}",
                f"{u:,}",
                f"{float(v) * 1000:.3f}",
                f"{float(c) * 1000:.3f}",
                f"{float(t0) * 1000:.3f}",
                f"{float(4 * t0) * 1000:.3f}",
            ]
        )
    links = []
    for name, label in (
        ("summary.csv", "汇总表 CSV"),
        ("runs.csv", "全部 96 次运行 CSV"),
        ("per_seed.csv", "逐种子结果 CSV"),
        ("classes.csv", "逐类逐次结果 CSV"),
        ("sa_controls.csv", "SA 对照 CSV"),
        ("raw_hashes.csv", "384 项原始文件 SHA"),
    ):
        links.append(
            download(
                name, label, (archive / name).read_bytes(), "text/csv;charset=utf-8"
            )
        )
    links.append(
        download(
            "requests.csv.gz",
            "全部 34,560 条请求记录 CSV.gz",
            gzip.compress((archive / "requests.csv").read_bytes(), mtime=0),
            "application/gzip",
        )
    )
    z = BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted((archive / "source").glob("*.json")):
            info = zipfile.ZipInfo(
                "sa-source/" + path.name, date_time=(2026, 10, 19, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, path.read_bytes())
    links.append(
        download(
            "SA-E26f-inputs-and-controls.zip",
            "SA 原始输入与对照 ZIP",
            z.getvalue(),
            "application/zip",
        )
    )
    links.append(
        download(
            "audit.json",
            "验收记录 JSON",
            (archive / "audit.json").read_bytes(),
            "application/json",
        )
    )
    md = (REPO / "SA-E26f对齐实验结果-20261019.md").read_bytes()
    links.append(
        download(
            "SA-E26f对齐实验结果-20261019.md",
            "完整文字报告 Markdown",
            md,
            "text/markdown;charset=utf-8",
        )
    )
    template = (PKG / "scripts/templates/sa_report.html").read_text()
    replacements = {
        "@@CORE_TABLE@@": table(
            [
                "工况",
                "SA FCFS",
                "SA TFS",
                "DV 直接 / 上限1",
                "DV TFS / 上限1",
                "DV 直接 / 上限8",
                "DV TFS / 上限8",
            ],
            core,
            "core-table",
        ),
        "@@SEED_TABLE@@": table(
            [
                "工况 / 输入种子",
                "SA FCFS",
                "SA TFS",
                "DV 直接1",
                "DV TFS1",
                "DV 直接8",
                "DV TFS8",
            ],
            seed_rows,
            "seed-table",
            lambda i, r: "418" in r[0],
        ),
        "@@ALL_TABLE@@": table(
            [
                "工况",
                "请求数上限",
                "准入策略",
                "期限达标率",
                "平均 TTFT / ms",
                "中央等待 / ms",
                "实际批均值",
                "完成全部请求 / s",
            ],
            all_rows,
            "all-results",
        ),
        "@@CLASS_TABLE@@": table(
            [
                "工况",
                "上限",
                "策略",
                "请求类",
                "期限达标率",
                "平均 TTFT / ms",
                "TTFT P95 / ms",
            ],
            class_rows,
            "all-classes",
        ),
        "@@RUN_TABLE@@": table(
            [
                "工况 / seed",
                "上限",
                "策略",
                "路由重复号",
                "期限达标率",
                "平均 TTFT / ms",
                "中央等待 / ms",
                "实际批均值",
                "实际批最大值",
            ],
            run_rows,
            "all-runs",
        ),
        "@@INPUT_TABLE@@": table(
            [
                "请求类",
                "远端命中 h / token",
                "重算 u / token",
                "每层读量 / MB",
                "每层计算 / ms",
                "参考 TTFT / ms",
                "期限宽度 / ms",
            ],
            shapes,
            "input-table",
        ),
        "@@DOWNLOADS@@": "".join(links),
        "@@DATA@@": json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace(
            "<", "\\u003c"
        ),
        "@@ORIGINAL@@": escape(md.decode()),
        "@@SA_COMMIT@@": escape(source["commit"]),
        "@@IMPL_SHA@@": escape(summary["implementation_sha256"]),
        "@@VERSIONS@@": escape(
            " · ".join(f"{k} {v}" for k, v in runtime["versions"].items())
        ),
        "@@SOURCE_TABLE@@": table(
            ["文件", "SHA256"],
            [[k, v] for k, v in source["source_sha256"].items()],
            "source-sha",
        ),
        "@@PARITY@@": table(
            ["模型对拍", "最大完成时间差 / s", "验收"],
            [
                [r["case"], f"{r['max_abs_error_s']:.3g}", "通过"]
                for r in json.loads((archive / "verification.json").read_text())
            ],
            "parity-table",
        ),
        "@@AUDIT_JSON@@": escape(json.dumps(audit, ensure_ascii=False, indent=2)),
    }
    for key, name in (
        ("A", "sa_alignment_A_timeline.png"),
        ("B", "sa_alignment_B_timeline.png"),
        ("OVERVIEW", "sa_alignment_attainment.png"),
        ("SPREAD", "sa_alignment_ttft_spread.png"),
    ):
        replacements["@@IMG_" + key + "@@"] = (
            "data:image/png;base64,"
            + base64.b64encode((figures / name).read_bytes()).decode()
        )
    for key, value in replacements.items():
        assert key in template, key
        template = template.replace(key, value)
    assert "@@" not in template, "unresolved template value"
    output.write_text(template)
    print(
        json.dumps(
            {
                "output": str(output),
                "bytes": output.stat().st_size,
                "runs": len(runs),
                "aggregate_rows": len(all_rows),
                "class_rows": len(class_rows),
                "original_sha256": hashlib.sha256(md).hexdigest(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--archive", type=Path, default=PKG / "results/sa_alignment_summary_20261019"
    )
    p.add_argument(
        "--figures", type=Path, default=REPO / "docs/figures/sa_alignment_20261019"
    )
    p.add_argument(
        "--output", type=Path, default=REPO / "SA-E26f对齐实验报告-20261019.html"
    )
    a = p.parse_args()
    build(a.archive.resolve(), a.figures.resolve(), a.output.resolve())
