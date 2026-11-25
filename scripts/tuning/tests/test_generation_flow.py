"""Drive one full generation with fake model outputs and fake replays.

No WSL, no opencode: ``start_task`` writes plausible outputs for each role, and
a helper injects run results for whatever the search enqueues. The test proves
the state machine reaches a promotion and starts the next generation.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from tuning import config, daemon as core, report, wsl  # noqa: E402
from tuning.search import Search  # noqa: E402
from tuning.store import read_json, write_json  # noqa: E402

W = list(config.WORKLOADS)
LIGHT = dict(ttft=[2.0, 2.1, 1.9, 2.05, 1.95], makespan=[10.0, 10.1, 9.9, 10.05, 9.95])
DIGEST = dict(overload=dict(overload_s=4.0, near_overload_s=1.0, peak_demand_GBs=40.0, link_utilization=0.7,
                            layer_overload_s=3.0, saturated_s=2.0, read_stretch_median=2.0, read_stretch_p95=8.0,
                            link_time_s=5.0, transfer_span_s=7.0),
              workers=dict(count=4, busy_s=40.0, window_s=48.0, idle_s=8.0, idle_mean_s=2.0, starved_idle_s=3.0, longest_idle_s=0.2),
              read_wait_s=9.0, model_sum_s=36.0, reference=dict(mean_ttft_s=0.8, makespan_s=9.0, compute_floor_s=9.0, link_floor_s=5.0),
              permits=dict(heavy_hints=100, grants=100, denied=10, peak_held=2, blocked_requests=5, blocked_total_s=4.0, blocked_max_s=1.0),
              waits=dict(admission_mean_s=0.05, admission_max_s=0.5), pressure_GBs=dict(zero_read_share=0.3, min=0.1, median=4.0, p75=8.0, max=40.0, over_half_link=0.1),
              classes={}, timeline=dict(bin_s=0.25, demand_GBs=[1.0], active_requests=[2.0]))


def digest_with(overload=4.0, idle=8.0):
    digest = json.loads(json.dumps(DIGEST))
    digest["overload"]["overload_s"] = overload
    digest["workers"]["idle_s"] = idle
    return digest


def card(name="带宽预算账本", **changes):
    value = dict(name=name, tier=0,
                 mechanism=dict(signal=["demand_sum"], action=["admit", "hold"], timing=["schedule"], feedback=["closed_loop"]),
                 params={"admission.params.util": dict(value=0.9, min=0.5, max=1.0)},
                 causal="在读需求之和超过链路带宽的一个比例时扣住新的重读，减少争用造成的等读时间，代价是可能让实例空转。",
                 signature=[dict(workload="*", metric="overload_s", direction="down")],
                 failure="当所有请求都是重读且实例很少时可能全部被挡住，造成空转和更长的完成时间，需要回填规则保护。",
                 protection="等待超过阈值的请求优先放行一条，避免饿死和死锁情况的发生，保护机制必须足够简单可靠。")
    value.update(changes)
    return value


class GenerationFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.d = core.Daemon(self.tmp)
        self.d.state = core.initial_state("deadbeef")
        state = self.d.state
        state.update(phase="run", gen=dict(n=0, step="open"))
        self.run_seq = 0
        for workload in W:
            state["baseline"][workload] = dict(
                light=dict(ttft=list(LIGHT["ttft"]), makespan=list(LIGHT["makespan"]),
                           runs=[self.mkrun(workload, t, m, digest_with()) for t, m in zip(LIGHT["ttft"], LIGHT["makespan"])]),
                tfs=dict(ttft=[1.9], makespan=[10.2], runs=[]))
        for stress in config.STRESS_SPECS:
            state["stress_baseline"][stress] = dict(light=dict(ttft=[2.0, 2.1, 1.9], makespan=[10.0, 10.1, 9.9],
                                                               runs=[self.mkrun(stress, 2.0, 10.0, digest_with())]))
        state["stress"] = dict(built=True)
        package = self.d.base_dir / "dynamo_vllm_cpu_sim" / "sim" / "stack"
        package.mkdir(parents=True)
        (package / "read_admission.py").write_text('"""Frozen arrival classification."""\nimport uuid\n', encoding="utf-8")
        self.enqueued = []
        self.bad_lab = None
        self.tool_request = "一个按负载汇总候选成绩的脚本"
        self.tool_script = "print('load ttft makespan')\n"
        # fakes
        self.d.start_task = self.fake_task
        Search.enqueue = self.fake_enqueue
        wsl.candidate_tests = lambda folder, tier: ("pass", "Ran 1 test ... OK")
        wsl.cleanup_runs = lambda keep: "1G"
        wsl.disk_used = lambda: "1G"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkrun(self, workload, ttft, makespan, digest):
        self.run_seq += 1
        run_id = "t%04d" % self.run_seq
        write_json(self.tmp / "runs" / run_id / "result.json", dict(status="done", summary=dict(mean_ttft_s=ttft, makespan_s=makespan), digest=digest))
        return run_id

    def fake_enqueue(self, cand_id, tag, rounds, workloads=None, stress=False, exclusive=None):
        if stress:
            self.d.state["candidates"][cand_id]["attack_specs"] = list(workloads or [])
        self.d.state["candidates"][cand_id]["queued"][core.queued_key(tag, "x")] = 1
        for workload in workloads or W:
            self.enqueued.append((cand_id, tag, rounds, workload, stress))

    def fake_task(self, task_id, role, task_md, write_paths, shell, **options):
        """Do what a cooperative model would do, then mark the task ok."""
        state = self.d.state
        gen = state["gen"]
        ws = self.tmp
        if task_id.startswith("analyst_g"):
            write_text(ws / "digest" / ("gen_%d" % gen["n"]) / "bottlenecks.md", "# 瓶颈卡\n\n408 上主要是链路争用。\n")
        elif task_id.startswith(("proposer_g", "revise_")):
            lab = task_id.split("_")[-1] if task_id.startswith("proposer") else None
            cand_id = next(c for c in gen["llm"] if state["candidates"][c].get("lab") == lab) if lab else task_id.split("_")[1]
            folder = ws / "candidates" / cand_id
            good = self.bad_lab != lab
            self.write_candidate(folder, good)
            state["candidates"][cand_id].update(quality="good" if good else "bad")
        elif task_id.startswith("redteam_"):
            cand_id = task_id.split("_")[1]
            write_json(ws / "reviews" / cand_id / "review.json", dict(cheat_paths=[], case_specific=[], risks=[], zero_findings=False,
                                                                      verdict="pass", required_fixes=[], attack=dict(name="bw10_540"), why=""))
        elif task_id.startswith("crossval_"):
            write_json(ws / task_id.replace("crossval_", "crossval/").replace("_c", "_c").split(".json")[0] if False else ws / write_paths[0],
                       dict(opinion="看起来成立", agree=True, recommend_more_runs=False, concerns=[]))
        elif task_id.startswith("auditor_"):
            cand_id = task_id.split("_")[1]
            cand = state["candidates"][cand_id]
            rows = [r for w in W for r in cand["runs"][w]["confirm"]]
            write_json(ws / "audits" / cand_id / "audit.json", dict(
                recomputed={w: dict(mean_ttft_s=sum(x["ttft"] for x in cand["runs"][w]["confirm"]) / len(cand["runs"][w]["confirm"]),
                                    makespan_s=sum(x["makespan"] for x in cand["runs"][w]["confirm"]) / len(cand["runs"][w]["confirm"])) for w in W},
                method="mean", agrees=True, notes=""))
        elif task_id.startswith("judge_"):
            cand_id = task_id.split("_")[1]
            write_json(ws / "judgements" / cand_id / "judge.json", dict(generality=2, causal=2, evidence=2, issues=[], comment=""))
        elif task_id.startswith("meta_g"):
            write_json(ws / "meta" / "amendments" / ("gen_%d.json" % gen["n"]),
                       dict(lessons_md="第一代：带宽预算账本有效。", hypotheses=[dict(family="bandwidth_budget", status="tested", priority=5, note="")],
                            proposer_hints="继续沿带宽账本深挖。", redteam_hints="", allocation=dict(llm=2, param=2),
                            tool_requests=[self.tool_request]))
        elif task_id.startswith("toolmaker_"):
            name = "tool_g%d" % gen["n"]
            write_text(ws / "tools" / name / "run.py", self.tool_script)
            write_text(ws / "tools" / name / "README.md", "# 工具\n\n样例。\n")
        elif task_id.startswith("prover_g"):
            write_json(ws / "digest" / ("gen_%d" % gen["n"]) / "untested.json", dict(directions=[dict(cell="bytes/route/schedule/open_loop", idea="", why="")]))
        else:
            raise AssertionError("unexpected task %s" % task_id)
        state["tasks"][task_id] = dict(status="ok", role=role, attempts=1, error="", duration_s=1.0, steps=1, tool_calls=1)

    def write_candidate(self, folder, good):
        (folder / "overlay" / "sim" / "stack").mkdir(parents=True, exist_ok=True)
        (folder / "tests").mkdir(exist_ok=True)
        (folder / "overlay" / "sim" / "stack" / "read_admission.py").write_text('"""Frozen arrival classification."""\nimport uuid\n\nBUDGET = 1\n', encoding="utf-8")
        (folder / "tests" / "test_scale_invariance.py").write_text("import unittest\n", encoding="utf-8")
        write_json(folder / "policy.json", {"admission": {"params": {"util": 0.9}}})
        write_json(folder / "card.json", card("带宽预算账本" if good else "坏主意"))
        (folder / "CARD.md").write_text("# 卡\n\n说明。\n", encoding="utf-8")

    def feed(self):
        """Turn every enqueued replay into a finished run with the candidate's quality."""
        while self.enqueued:
            cand_id, tag, rounds, workload, stress = self.enqueued.pop(0)
            cand = self.d.state["candidates"][cand_id]
            if stress:
                rows = cand["runs"].setdefault(workload, {}).setdefault(tag, [])
                light = self.d.state["stress_baseline"][workload]["light"]
                factor = 0.8 if cand.get("quality", "good") == "good" else 1.2
                while len(rows) < rounds:
                    ttft, makespan = light["ttft"][len(rows) % 3] * factor, light["makespan"][len(rows) % 3] * factor
                    rows.append(dict(run=self.mkrun(workload, ttft, makespan, digest_with()), ttft=ttft, makespan=makespan, stress=workload))
                continue
            rows = cand["runs"].setdefault(workload, {}).setdefault(tag, [])
            factor_ttft, factor_mk = (0.8, 0.9) if cand.get("quality", "good") == "good" else (1.3, 1.2)
            while len(rows) < rounds:
                base = self.d.state["baseline"][workload]["light"]
                ttft = base["ttft"][len(rows) % 5] * factor_ttft
                makespan = base["makespan"][len(rows) % 5] * factor_mk
                digest = digest_with(overload=1.0 if cand.get("quality") == "good" else 6.0)
                rows.append(dict(run=self.mkrun(workload, ttft, makespan, digest), ttft=ttft, makespan=makespan, stress=None))

    def drive(self, limit=400):
        search = Search(self.d)
        for _ in range(limit):
            search.step()
            json.dumps(self.d.state)  # the state is JSON on disk; a tuple key crashed the daemon once
            self.feed()
            if self.d.state.get("history"):
                return
        self.fail("generation did not finish: step=%s statuses=%s" % (
            self.d.state["gen"]["step"], {c: r["status"] for c, r in self.d.state["candidates"].items()}))

    def test_generation_promotes_the_good_candidate_and_rejects_the_bad_one(self):
        self.bad_lab = "B"
        self.drive()
        state = self.d.state
        good = next(c for c, r in state["candidates"].items() if r.get("quality") == "good")
        bad = next(c for c, r in state["candidates"].items() if r.get("quality") == "bad")
        self.assertEqual(state["champion"], good)
        self.assertEqual(state["candidates"][good]["status"], "champion")
        self.assertEqual(state["candidates"][bad]["status"], "rejected")
        self.assertTrue(state["candidates"][bad]["reason"])
        self.assertEqual(len(state["champion_history"]), 1)
        self.assertEqual(len(state["history"]), 1)
        self.assertTrue(state["map"])
        self.assertEqual(state["meta"]["lessons_md"], "第一代：带宽预算账本有效。")
        self.assertEqual(state["gen"], dict(n=1, step="open", opened=False))
        self.assertTrue(state["tools"]["tool_g1"]["ok"], state["tools"])
        self.assertIn("python tools/*", Search(self.d).read_shell)
        # the next generation opens: two new proposals, and parameter children of the champion
        Search(self.d).step_open()
        self.assertEqual(len(state["gen"]["llm"]), 2)
        self.assertEqual(len([c for c in state["candidates"].values() if c["kind"] == "param" and c["gen"] == 2]),
                         state["meta"]["allocation"]["param"])
        (self.tmp / "dashboard").mkdir(exist_ok=True)
        report.Dashboard(self.d).render()
        self.assertIn("带宽预算账本", (self.tmp / "dashboard" / "index.html").read_text(encoding="utf-8"))

    def test_generation_survives_when_every_proposal_fails(self):
        self.bad_lab = "A"
        other = "B"
        original = self.fake_task

        def failing(task_id, role, task_md, write_paths, shell, **options):
            if task_id.startswith("proposer_g1_%s" % other):
                self.d.state["tasks"][task_id] = dict(status="error", role=role, attempts=5, error="model refused", duration_s=1.0, steps=1, tool_calls=1)
                return
            original(task_id, role, task_md, write_paths, shell, **options)

        self.d.start_task = failing
        search = Search(self.d)
        for _ in range(100):
            search.step()
            self.feed()
            if self.d.state.get("history"):
                break
        first = [c for c, r in self.d.state["candidates"].items() if r.get("gen") == 1]
        self.assertEqual(len(first), 2)
        self.assertEqual({self.d.state["candidates"][c]["status"] for c in first}, {"rejected"})
        self.assertIsNone(self.d.state["champion"])
        self.assertEqual(self.d.state["stagnation"], 1)


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()


class ToolmakerFlow(GenerationFlow):
    def test_a_tool_that_does_not_run_is_not_enabled(self):
        self.tool_script = "raise SystemExit(3)\n"
        self.drive()
        self.assertFalse(self.d.state["tools"]["tool_g1"]["ok"])
        self.assertNotIn("python tools/*", Search(self.d).read_shell)
        self.assertTrue(any("没有跑通" in a["text"] for a in self.d.state["alerts"]))


