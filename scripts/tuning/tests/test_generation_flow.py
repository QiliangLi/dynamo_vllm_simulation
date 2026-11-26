"""Drive one full generation with fake model outputs and fake replays.

No WSL, no opencode: ``start_task`` writes plausible outputs for each role, and
a helper injects run results for whatever the search enqueues. The test proves
the state machine reaches a promotion and starts the next generation.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from tuning import config, daemon as core, report, wsl  # noqa: E402
from tuning.search import Search, finding_text, normalize_overlay  # noqa: E402
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
        self.event_total = None   # ledger events of a smoke replay, when the fake digest reports them
        self.smoke_seen = []
        self.redteam_verdict = None   # what the fake red team writes, on top of its default "pass"
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
            if tag == "smoke":
                self.smoke_seen.append((cand_id, tag, rounds, workload, stress))
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
            review = dict(cheat_paths=[], case_specific=[], risks=[], zero_findings=False,
                          verdict="pass", required_fixes=[], attack=dict(name="bw10_540"), why="")
            review.update(self.redteam_verdict or {})
            write_json(ws / "reviews" / cand_id / "review.json", review)
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
                if tag == "smoke" and self.event_total:
                    digest["admission_events"] = dict(total=self.event_total, cap=10000, kept=self.event_total, capped=False)
                rows.append(dict(run=self.mkrun(workload, ttft, makespan, digest), ttft=ttft, makespan=makespan, stress=None))

    def test_a_candidate_near_the_ledger_event_cap_goes_back_before_any_screening_run(self):
        """On 540 a deny-heavy candidate passed 7 replays in 8 and failed the eighth: past 10000 ledger
        events the checker cannot join the workers' load records any more."""
        self.event_total = 9600
        search = Search(self.d)
        why = "账本事件量越线风险"
        for _ in range(200):
            search.step()
            self.feed()
            if any(self.d.state["candidates"][c].get("fix_why") == why for c in self.d.state["gen"]["llm"]):
                break
        sent = [self.d.state["candidates"][c] for c in self.d.state["gen"]["llm"] if self.d.state["candidates"][c].get("fix_why") == why]
        self.assertEqual(len(sent), 2)
        for cand in sent:
            self.assertEqual(cand["status"], "revising")
            self.assertIn("540 上 9600 条", json.dumps(cand["fix_issues"], ensure_ascii=False))
        self.assertFalse([e for e in self.enqueued if e[1] == "screen"])
        self.assertFalse([c for c in self.d.state["candidates"].values() if c.get("runs", {}).get("408", {}).get("screen")])
        # Once the proposer has brought the event count down, the generation completes as usual.
        self.event_total = None
        for _ in range(400):
            search.step()
            self.feed()
            if self.d.state.get("history"):
                break
        self.assertTrue(self.d.state.get("history"), self.d.state["gen"])
        smoked = sorted({workload for _, tag, _, workload, _ in self.smoke_seen})
        self.assertEqual(smoked, ["408", "540"])

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

    def test_the_meta_strategist_decides_how_many_proposal_groups_open(self):
        """Its task book offers allocation llm 1–3; generation 1 asked for three and still got two."""
        original = self.fake_task

        def asking_for(task_id, role, task_md, write_paths, shell, **options):
            original(task_id, role, task_md, write_paths, shell, **options)
            if task_id.startswith("meta_g"):
                path = self.tmp / "meta" / "amendments" / "gen_1.json"
                amendment = read_json(path)
                amendment["allocation"] = dict(llm=3, param=0)
                write_json(path, amendment)
        self.d.start_task = asking_for
        self.drive()
        state = self.d.state
        self.assertEqual(state["meta"]["allocation"], dict(llm=3, param=0))
        search = Search(self.d)
        search.step_open()
        search.step_analyst()
        search.step_analyst()
        search.step_propose()
        search.step_propose()
        self.assertEqual(state["gen"]["llm"], ["c0003", "c0004", "c0005"])
        self.assertEqual([state["candidates"][c]["lab"] for c in state["gen"]["llm"]], ["A", "B", "C"])
        self.assertEqual([state["candidates"][c]["status"] for c in state["gen"]["llm"]], ["drafted"] * 3)
        lenses = [report.roles_lens(state, lab) for lab in ("A", "B", "C")]
        self.assertEqual(len(set(lenses)), 3)
        for lab, lens in zip(("A", "B", "C"), lenses):
            self.assertIn(lens, (self.tmp / "digest" / "gen_2" / ("context_%s.md" % lab)).read_text(encoding="utf-8"))
        # One group only when that is what was asked for.
        state["meta"]["allocation"] = dict(llm=1, param=0)
        state["gen"] = dict(n=2, step="open", opened=False)
        search.step_open()
        self.assertEqual(state["gen"]["llm"], ["c0006"])

    def test_cross_validation_can_be_switched_off(self):
        """kimi-k3.1-pro ran out of quota; the design's fallback is to skip the two points it sits at."""
        enabled, model = config.CROSSVAL_ENABLED, config.MODELS["crossval"]
        config.CROSSVAL_ENABLED = False
        try:
            search = Search(self.d)
            self.assertIsNone(search.crossval("crossval_review_c0001_0", "问题", [], "crossval/review_c0001.json"))
            self.assertEqual(self.d.state["tasks"], {})
        finally:
            config.CROSSVAL_ENABLED, config.MODELS["crossval"] = enabled, model
        self.assertNotIn("moonshot", config.MODELS["crossval"])
        self.assertEqual(config.NON_GLM_ROLES, frozenset())

    def test_a_red_team_rejection_with_structured_findings_rejects_the_candidate_and_the_generation_goes_on(self):
        """The first real rejection joined the findings as if they were strings; the step raised on every
        pass and the generation stood still. Findings are objects, or strings, or both."""
        self.redteam_verdict = dict(
            verdict="reject",
            case_specific=[dict(id="CS1", severity="高", kind="代码事实", point="常数按负载类的均值拟合", evidence=[], consequence="换负载就失效"),
                           dict(id="CS2", severity="中", point="没有推导"), "一条纯文本的发现"],
            risks=[dict(risk="空转上升")], cheat_paths=[])
        self.drive()
        state = self.d.state
        first = [c for c, r in state["candidates"].items() if r.get("gen") == 1]
        self.assertEqual([state["candidates"][c]["status"] for c in first], ["rejected", "rejected"])
        for cand_id in first:
            self.assertIn("红队否决：CS1、高：常数按负载类的均值拟合；CS2、中：没有推导；一条纯文本的发现",
                          state["candidates"][cand_id]["reason"])
        self.assertEqual(len(state["history"]), 1)
        self.assertIsNone(state["champion"])
        text = finding_text
        self.assertEqual(text(dict(fix="改 test_scale.py 第 9 行")), "改 test_scale.py 第 9 行")
        self.assertEqual(text(dict(id="CS1", severity="high", title="slack 是计数", evidence=[])), "CS1、high：slack 是计数")
        self.assertEqual(text(dict(other=1)), '{"other": 1}')
        self.assertEqual(text(7), "7")

    def test_a_step_that_raises_only_stops_its_own_candidate(self):
        """The first real red-team rejection raised inside the step on every pass; the other two
        proposals of the generation stood still with it."""
        original = Search.at_screen

        def raising(self_, cand_id):
            if cand_id == "c0002":   # the bad proposal of this test: its screening step is the broken one
                raise TypeError("sequence item 0: expected str instance, dict found")
            return original(self_, cand_id)
        Search.at_screen = raising
        try:
            self.bad_lab = "B"
            self.drive()
        finally:
            Search.at_screen = raising.__wrapped__ if hasattr(raising, "__wrapped__") else original
        state = self.d.state
        stuck = state["candidates"]["c0002"]
        self.assertEqual(stuck["status"], "stuck")
        self.assertEqual(stuck["resume_status"], "screen")
        self.assertEqual(stuck["step_errors"], 5)
        self.assertIn("TypeError('sequence item 0: expected str", stuck["reason"])
        self.assertTrue(any("被搁置" in a["text"] for a in state["alerts"]))
        # The other proposal was not held up: it was screened and promoted.
        self.assertEqual(state["champion"], "c0001")
        self.assertEqual(len(state["history"]), 1)

    def test_proposals_that_finish_after_the_last_group_are_still_picked_up(self):
        """Generation 4 lost two of its three proposals: the step moved on as soon as the last group it
        looked at had finished, and a candidate still being drafted does not count as active."""
        self.d.state["meta"]["allocation"] = dict(llm=3, param=0)
        finish_on = {"proposer_g1_A": 4, "proposer_g1_B": 6}   # the third group finishes on its first call
        original, calls = self.fake_task, Counter()

        def slow(task_id, role, task_md, write_paths, shell, **options):
            calls[task_id] += 1
            if calls[task_id] < finish_on.get(task_id, 1):
                self.d.state["tasks"][task_id] = dict(status="running", role=role, attempts=1, error="")
                return
            original(task_id, role, task_md, write_paths, shell, **options)
        self.d.start_task = slow
        self.drive()
        state = self.d.state
        first = [c for c, r in state["candidates"].items() if r.get("gen") == 1]
        self.assertEqual(len(first), 3)
        self.assertEqual([state["candidates"][c]["status"] for c in first].count("drafting"), 0)
        for cand_id in first:
            self.assertIn("redteam_%s_0" % cand_id, state["tasks"])
        self.assertGreaterEqual(calls["proposer_g1_A"], 4)
        self.assertGreaterEqual(calls["proposer_g1_B"], 6)

    def test_overlay_files_written_one_directory_too_deep_are_moved_before_the_gate(self):
        """Proposers cannot delete what they wrote, so the gate kept refusing the misplaced copies."""
        root = Path(tempfile.mkdtemp())
        try:
            overlay = root / "overlay"
            deep = overlay / "dynamo_vllm_cpu_sim" / "sim" / "stack"
            deep.mkdir(parents=True)
            (overlay / "sim" / "stack").mkdir(parents=True)
            (deep / "read_admission.py").write_text("old", encoding="utf-8")
            (overlay / "sim" / "stack" / "read_admission.py").write_text("newer", encoding="utf-8")
            (deep / "policy").mkdir()
            (deep / "policy" / "budget.py").write_text("budget", encoding="utf-8")
            os.utime(deep / "read_admission.py", (1, 1))
            os.utime(overlay / "sim" / "stack" / "read_admission.py", (time.time(), time.time()))

            done = normalize_overlay(root)
            self.assertEqual(sorted(done), ["dropped dynamo_vllm_cpu_sim/sim/stack/read_admission.py (the copy at the right path is newer or equal)",
                                            "moved dynamo_vllm_cpu_sim/sim/stack/policy/budget.py to sim/stack/policy/budget.py"])
            self.assertFalse((overlay / "dynamo_vllm_cpu_sim").exists())
            self.assertEqual((overlay / "sim" / "stack" / "read_admission.py").read_text(encoding="utf-8"), "newer")
            self.assertEqual((overlay / "sim" / "stack" / "policy" / "budget.py").read_text(encoding="utf-8"), "budget")
            self.assertEqual(normalize_overlay(root), [])

            # The deeper copy wins when it is the newer one.
            deep.mkdir(parents=True)
            (deep / "late.py").write_text("latest", encoding="utf-8")
            (overlay / "sim" / "stack" / "late.py").write_text("earlier", encoding="utf-8")
            os.utime(overlay / "sim" / "stack" / "late.py", (1, 1))
            normalize_overlay(root)
            self.assertEqual((overlay / "sim" / "stack" / "late.py").read_text(encoding="utf-8"), "latest")
        finally:
            shutil.rmtree(root, ignore_errors=True)

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


