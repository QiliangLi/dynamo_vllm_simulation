"""进程栈集成验收断言（W6，对照 check_results.py 的 DES 角色）。

用法（在 dynamo_vllm_cpu_sim/ 下，跑完 launch_stack + replay 之后）::

    .venv/bin/python scripts/check_stack.py results/stack_smoke results/stack_remote results/stack_slow

断言（全部来自真实运行的 report.json，禁止手写数值）：
- 每个目录：全部请求 ok=True 且 finish=length（输出按 max_tokens 停止）；
- stack_remote（远端 KV）：n_skipped=0；
- 同时给出 stack_remote 与 stack_slow 时：慢链路 TTFT 均值显著更大（A2 定性复现）。
"""

import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    report = json.loads((path / "report.json").read_text())
    for rid, r in report["requests"].items():
        if not r.get("ok"):
            raise AssertionError(f"{path.name}: {rid} not ok: {r.get('error')}")
        if r.get("finish") != "length":
            raise AssertionError(f"{path.name}: {rid} finish={r.get('finish')}")
    return report


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    reports = {Path(p).name: load(Path(p)) for p in argv}
    means = {}
    for name, rep in reports.items():
        ttfts = [r["ttft_s"] for r in rep["requests"].values() if r.get("ttft_s")]
        assert len(ttfts) == rep["n_ok"], f"{name}: missing ttft entries"
        means[name] = sum(ttfts) / len(ttfts)
        print(
            f"{name}: ok={rep['n_ok']}/{rep['n_total']} skipped={rep['n_skipped']} "
            f"ttft_mean={means[name] * 1000:.1f}ms"
        )
    remote = reports.get("stack_remote")
    if remote is not None:
        assert remote["n_skipped"] == 0, "stack_remote 应为可编码 trace（无损失跳过）"
    slow = reports.get("stack_slow")
    if remote is not None and slow is not None:
        assert means["stack_slow"] > 3 * means["stack_remote"], (
            f"A2 慢存储对照未复现: slow={means['stack_slow']} fast={means['stack_remote']}"
        )
        print(
            f"A2 slow/fast ttft ratio = {means['stack_slow'] / means['stack_remote']:.1f}x (>3x required)"
        )
    print("stack checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
