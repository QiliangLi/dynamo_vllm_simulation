"""Unit tests of the tuning workflow that need neither WSL nor opencode.

Run from scripts/tuning:  python -m unittest discover -s tests
"""
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from tuning import config, daemon as core, evaluator, gates, report, scope, store  # noqa: E402
from tuning.store import write_json  # noqa: E402

W = list(config.WORKLOADS)


class EvaluatorTests(unittest.TestCase):
    def test_improvement_and_bound(self):
        reference = dict(mean=10.0, se=0.1)
        candidate = dict(mean=9.0, se=0.1)
        self.assertAlmostEqual(evaluator.improvement(reference, candidate), 0.10)
        # 10% improvement, 95% lower bound is 10% - 1.645 * sqrt(0.02) / 10
        self.assertAlmostEqual(evaluator.improvement_lb(reference, candidate), 0.10 - 1.645 * 0.1414213562 / 10)

    def test_regression_allows_noise(self):
        reference = dict(mean=10.0, se=0.2)
        self.assertFalse(evaluator.regression(reference, dict(mean=10.2, se=0.2)))
        self.assertTrue(evaluator.regression(reference, dict(mean=10.9, se=0.2)))

    def _series(self, ttft, makespan):
        return {w: dict(ttft=list(ttft), makespan=list(makespan)) for w in W}

    def test_promotion_needs_confident_gain_without_regression(self):
        reference = self._series([2.0, 2.1, 1.9], [10.0, 10.2, 9.8])
        good = self._series([1.5, 1.6, 1.4], [9.0, 9.2, 8.8])
        self.assertTrue(evaluator.decide_promotion(evaluator.workload_result(reference, good))["promote"])
        noisy = self._series([2.0, 2.1, 1.9], [10.0, 10.2, 9.8])
        self.assertFalse(evaluator.decide_promotion(evaluator.workload_result(reference, noisy))["promote"])
        mixed = {w: dict(ttft=[1.5, 1.6, 1.4], makespan=[9.0, 9.2, 8.8]) for w in W}
        mixed[W[1]] = dict(ttft=[2.6, 2.7, 2.5], makespan=[12.0, 12.2, 11.8])
        decision = evaluator.decide_promotion(evaluator.workload_result(reference, mixed))
        self.assertFalse(decision["promote"])
        self.assertEqual(decision["regressions"], [W[1]])

    def test_score_balances_mean_and_worst(self):
        reference = self._series([2.0], [10.0])
        candidate = self._series([1.0], [9.0])
        results = evaluator.workload_result(reference, candidate)
        # mean TTFT: 2.0 -> 1.0 is 50%; makespan: 10 -> 9 is 10%; per workload 30%, five equal workloads.
        self.assertAlmostEqual(evaluator.total_score(results), 0.30)

    def test_screen_pass_needs_both_directions(self):
        reference = self._series([2.0], [10.0])
        worse = self._series([2.2], [9.0])
        self.assertFalse(evaluator.screen_pass(evaluator.workload_result(reference, worse))[0])
        better = self._series([1.8], [9.0])
        self.assertTrue(evaluator.screen_pass(evaluator.workload_result(reference, better))[0])

    def test_control_outside_three_sigma(self):
        self.assertEqual(evaluator.control_ok(dict(ttft=[2.0, 2.1, 1.9, 2.05], makespan=[10, 10.1, 9.9, 10]), 2.05, 10.0), [])
        self.assertEqual(evaluator.control_ok(dict(ttft=[2.0, 2.1, 1.9, 2.05], makespan=[10, 10.1, 9.9, 10]), 2.6, 10.0), ["ttft"])


class ScopeTests(unittest.TestCase):
    def test_tier1_paths(self):
        self.assertEqual(scope.check_overlay_paths(["sim/admission.py", "sim/stack/policy/budget.py"], 0), [])
        self.assertEqual(scope.check_overlay_paths(["sim/storage.py"], 0), ["sim/storage.py"])
        self.assertEqual(scope.check_overlay_paths(["../x.py"], 0), ["../x.py"])
        self.assertEqual(scope.check_overlay_paths(["sim/stack/layer_storage.py"], 1), [])
        self.assertEqual(scope.check_overlay_paths(["sim/priority.py"], 1), ["sim/priority.py"])
        self.assertEqual(scope.check_overlay_paths(["sim/priority.py"], 2), [])

    def test_policy_keys(self):
        self.assertEqual(scope.check_policy({"admission": {}, "routing_accounting": {}}, 0), [])
        self.assertEqual(scope.check_policy({"storage": {}}, 0), ["storage"])
        self.assertEqual(scope.check_policy({"request_ordering": "edf"}, 2), [])
        self.assertEqual(scope.check_policy({"request_ordering": "edf"}, 1), ["request_ordering"])

    def test_forbidden_lines(self):
        hard, allowed = scope.forbidden_lines(["x = 1", "label = row['class_id']", "os.environ['X']  # tuning-allow: read config"])
        self.assertEqual(hard, ["label = row['class_id']"])
        self.assertEqual(allowed, ["os.environ['X']  # tuning-allow: read config"])

    def test_added_lines_and_protected_functions(self):
        base = "def keep():\n    return 1\n\ndef other():\n    return 2\n"
        new = "def keep():\n    return 1\n\ndef other():\n    return 3\n"
        self.assertEqual(scope.added_source_lines(base, new), ["    return 3"])
        path = "sim/stack/scheduler_admission.py"
        self.assertEqual(scope.protected_functions_changed("def patch_schedule(x):\n    return 1\n", "def patch_schedule(x):\n    return 1\n", path), [])
        self.assertEqual(scope.protected_functions_changed("def patch_schedule(x):\n    return 1\n", "def patch_schedule(x):\n    return 2\n", path), ["patch_schedule"])

    def test_every_scope_file_exists_in_the_frozen_base(self):
        base = HERE.parents[1] / "dynamo_vllm_cpu_sim"
        if not base.exists():
            self.skipTest("package sources not next to scripts/tuning")
        missing = [f for f in scope.TIER1_FILES if not (base / f).exists()]
        missing += [f for tier in scope.TIER2_ORDER for f in scope.TIER2[tier]["files"] if not (base / f).exists()]
        self.assertEqual(missing, [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.base = self.tmp / "base"
        package = self.base / scope.PACKAGE / "sim" / "stack"
        package.mkdir(parents=True)
        (package / "read_admission.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.cand = self.tmp / "candidates" / "c0001"
        (self.cand / "overlay" / "sim" / "stack").mkdir(parents=True)
        (self.cand / "tests").mkdir()
        (self.cand / "overlay" / "sim" / "stack" / "read_admission.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.cand / "tests" / "test_scale_invariance.py").write_text("import unittest\n", encoding="utf-8")
        (self.cand / "policy.json").write_text(json.dumps({"admission": {"params": {"util": 0.9}}}), encoding="utf-8")
        (self.cand / "card.json").write_text(json.dumps(self.card()), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def card(self, **changes):
        card = dict(name="带宽账本", tier=0, mechanism=dict(signal=["demand_sum"], action=["admit"], timing=["schedule"], feedback=["closed_loop"]),
                    params={"admission.params.util": dict(value=0.9, min=0.5, max=1.0)},
                    causal="把在途需求之和控制在链路带宽的一个比例以内，减少争用造成的等待，代价是可能让实例空转，用回填保护。",
                    signature=[dict(workload="*", metric="overload_s", direction="down")],
                    failure="当所有请求都是重读且实例数很少时，可能全部被挡住，造成空转和更长的完成时间，需要回填规则。",
                    protection="等待超过阈值的请求优先放行一条，避免饿死和死锁情况的发生，保护机制必须足够简单。")
        card.update(changes)
        return card

    def write_card(self, **changes):
        (self.cand / "card.json").write_text(json.dumps(self.card(**changes)), encoding="utf-8")

    def test_clean_candidate_passes(self):
        result = gates.static_gate(self.cand, self.base, 0)
        self.assertTrue(result.passed, result.issues)

    def test_issues_are_reported(self):
        self.write_card(tier=2, mechanism=dict(signal=["magic"], action=["admit"], timing=["schedule"], feedback=["closed_loop"]))
        (self.cand / "overlay" / "sim" / "storage.py").write_text("X = 1\n", encoding="utf-8")
        (self.cand / "overlay" / "sim" / "stack" / "read_admission.py").write_text("VALUE = row['class_id']\n", encoding="utf-8")
        result = gates.static_gate(self.cand, self.base, 0)
        self.assertFalse(result.passed)
        joined = " ".join(result.issues)
        for text in ("tier", "mechanism.signal", "sim/storage.py", "class_id"):
            self.assertIn(text, joined)

    def test_param_must_match_policy(self):
        self.write_card(params={"admission.params.util": dict(value=0.7, min=0.5, max=1.0)})
        result = gates.static_gate(self.cand, self.base, 0)
        self.assertFalse(result.passed)
        self.assertTrue(any("不一致" in issue for issue in result.issues))

    def test_scale_test_is_required(self):
        (self.cand / "tests" / "test_scale_invariance.py").unlink()
        (self.cand / "tests" / "test_budget.py").write_text("import unittest\n", encoding="utf-8")
        result = gates.static_gate(self.cand, self.base, 0)
        self.assertTrue(any("尺度不变" in issue for issue in result.issues))

    def test_manifest_detects_tampering(self):
        gates.write_manifest(self.cand)
        self.assertEqual(gates.manifest_changes(self.cand), [])
        (self.cand / "policy.json").write_text("{}", encoding="utf-8")
        self.assertEqual(gates.manifest_changes(self.cand), ["policy.json"])

    def test_signature_check(self):
        card = self.card(signature=[dict(workload="*", metric="overload_s", direction="down"), dict(workload="408", metric="idle_s", direction="up")])
        digest = lambda overload, idle: {"overload": {"overload_s": overload}, "workers": {"idle_s": idle}}  # noqa: E731
        pairs = {w: dict(cand=digest(1.0, 6.0), ref=digest(4.0, 3.0), cand_summary={"mean_ttft_s": 1, "makespan_s": 9}, ref_summary={"mean_ttft_s": 2, "makespan_s": 10}) for w in W}
        kept, missed = gates.signature_check(card, pairs)
        self.assertEqual(len(kept), 2)
        self.assertEqual(missed, [])
        pairs["408"] = dict(cand=digest(1.0, 3.0), ref=digest(4.0, 3.0), cand_summary={"mean_ttft_s": 1, "makespan_s": 9}, ref_summary={"mean_ttft_s": 2, "makespan_s": 10})
        kept, missed = gates.signature_check(card, pairs)
        self.assertEqual(len(kept), 1)
        self.assertTrue(missed[0].startswith("408"))


class ControlScriptTests(unittest.TestCase):
    def test_control_scripts_are_written_and_end_in_single_crlf(self):
        import tune
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            tune.write_controls(ws, "pythonw.exe")
            for name in ("暂停.cmd", "继续.cmd", "停止并收尾.cmd", "启动看板.cmd"):
                data = (ws / name).read_bytes()
                self.assertTrue(data.endswith(b"\r\n"), name)
                self.assertNotIn(b"\r\r\n", data, name)
            self.assertIn(b"type nul > control\\PAUSE", (ws / "暂停.cmd").read_bytes())
            self.assertIn(b"type nul > control\\STOP", (ws / "停止并收尾.cmd").read_bytes())
            self.assertIn(b"del control\\PAUSE", (ws / "继续.cmd").read_bytes())
            self.assertIn(b"dashboard\\index.html", (ws / "启动看板.cmd").read_bytes())
            supervisor = (ws / "守护.vbs").read_bytes()
            self.assertIn(b"pythonw.exe", supervisor)
            self.assertEqual(supervisor, (ws / "supervisor.vbs").read_bytes())


class FrozenHashTests(unittest.TestCase):
    def test_deploy_that_changes_a_frozen_file_is_reported(self):
        """A changed frozen file stopped the daemon at its next restart; the deploy said nothing."""
        import tune
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "tuning").mkdir()
            for name in core.FROZEN_FILES:
                (ws / "tuning" / name).write_bytes(b"first\n")
            write_json(ws / "state.json", dict(frozen=core.frozen_hashes_of(ws / "tuning")))
            self.assertEqual(tune.frozen_mismatch(ws), [])
            (ws / "tuning" / "config.py").write_bytes(b"second\n")
            self.assertEqual(tune.frozen_mismatch(ws), ["config.py"])
            self.assertEqual(tune.frozen_mismatch(ws / "nowhere"), [])


class DigestRefresh(unittest.TestCase):
    def test_the_daemons_digests_are_rewritten_from_the_state_and_not_tracked(self):
        """Tracked, the guard reverted the champion digest to "none" after the next model task."""
        import tune
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            (root / "digest" / "champion.md").write_text("被某个任务改过的内容", encoding="utf-8")
            daemon.refresh_digests()
            self.assertIn("现在还没有冠军", (root / "digest" / "champion.md").read_text(encoding="utf-8"))
            self.assertFalse((root / "digest" / "baseline.md").exists())
            for workload in config.WORKLOADS:
                daemon.state["baseline"][workload] = dict(light=dict(ttft=[2.0, 2.1], makespan=[10.0, 10.2], runs=[]))
            daemon.refresh_digests()
            self.assertIn("| ABC408 | 2 |", (root / "digest" / "baseline.md").read_text(encoding="utf-8"))
            for name in ("digest/baseline.md", "digest/champion.md"):
                self.assertIn(name + "\n", tune.IGNORE)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class PlannedExit(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.daemon = core.Daemon(self.root)
        self.daemon.state = core.initial_state("x")
        self.control = self.root / "control"
        self.control.mkdir(exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_same_error_ten_passes_in_a_row_asks_for_a_person(self):
        """Forty passes of any errors, with a backoff up to five minutes, was two hours of standing still."""
        for _ in range(9):
            self.assertFalse(self.daemon.note_error(TypeError("sequence item 0: expected str")))
        self.assertTrue(self.daemon.note_error(TypeError("sequence item 0: expected str")))
        # Different errors in turn are counted, but only the long threshold applies to them.
        other = core.Daemon(self.root)
        for index in range(39):
            self.assertFalse(other.note_error(ValueError("number %d" % index)))
        self.assertTrue(other.note_error(ValueError("number 39")))

    def test_a_restart_waits_for_the_model_tasks(self):
        """Killing a task loses half an hour of a proposal's work."""
        (self.control / "RESTART").write_text("")
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        self.daemon.threads["proposer_g1_A"] = worker
        self.assertIsNone(self.daemon.planned_exit())
        self.assertTrue((self.control / "RESTART").exists())
        release.set()
        worker.join()
        self.assertEqual(self.daemon.planned_exit(), "restart")
        self.assertFalse((self.control / "RESTART").exists())
        self.assertIsNone(self.daemon.planned_exit())

    def test_unfreeze_registers_what_is_on_disk(self):
        """A command that wrote the state file itself lost the race with the daemon's own saves."""
        self.daemon.state["frozen"] = dict(config_py="stale")
        (self.control / "UNFREEZE").write_text("")
        self.assertEqual(self.daemon.planned_exit(), "unfreeze")
        self.assertEqual(self.daemon.state["frozen"], core.frozen_hashes())
        self.assertFalse((self.control / "UNFREEZE").exists())

    def test_an_unfreeze_left_for_a_stopped_daemon_is_honoured_before_the_check(self):
        self.daemon.state["frozen"] = dict(config_py="stale")
        (self.control / "UNFREEZE").write_text("")
        self.assertTrue(self.daemon.register_frozen_at_start())
        self.daemon.verify_frozen()  # does not raise
        self.assertFalse((self.control / "UNFREEZE").exists())
        self.assertFalse(self.daemon.register_frozen_at_start())


class DigestTests(unittest.TestCase):
    def test_event_count_comes_from_the_full_log_and_says_when_the_snapshot_was_capped(self):
        """The snapshot keeps the last 10000 ledger events; the join in the checker needs the first ones."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("digest", HERE / "wsl" / "digest.py")
        digest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(digest)
        root = Path(tempfile.mkdtemp())
        try:
            (root / "admission.events.jsonl").write_text('{"event": "a"}\n' * 10001 + '{"event": "last"}\n', encoding="utf-8")
            final = dict(events=[dict(event="late")] * 10000)
            self.assertEqual(digest.admission_events(root, final), dict(total=10002, cap=10000, kept=10000, capped=True))
            self.assertEqual(digest.admission_events(Path(root) / "nowhere", dict(events=[1, 2])), dict(total=2, cap=10000, kept=2, capped=False))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ModelRecordTests(unittest.TestCase):
    def test_the_model_a_task_ran_on_is_read_from_opencode_not_assumed_from_the_role(self):
        """The provider is the user's gateway, called "kimi" for GLM too; opencode's own default is a Kimi model."""
        import sqlite3
        from tuning import llm
        root = Path(tempfile.mkdtemp())
        try:
            db = root / "opencode.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
            connection.execute("CREATE TABLE session_v2 (id TEXT, model TEXT)")
            glm = dict(providerID="kimi", id="zhipu/glm-5.4", variant="high")
            kimi = dict(providerID="kimi", id="moonshot/kimi-k3.1-pro")
            rows = [("ses_a", 100_000, glm), ("ses_a", 200_000, glm), ("ses_a", 900_000, kimi),   # the last one is a later task's
                    ("ses_b", 100_000, kimi), ("ses_c", 100_000, None)]
            for index, (session, created, model) in enumerate(rows):
                connection.execute("INSERT INTO session_message VALUES (?, ?, 'assistant', ?, ?, 0, ?)",
                                   (str(index), session, index, created, json.dumps(dict(role="assistant", model=model))))
            connection.execute("INSERT INTO session_v2 VALUES ('ses_a', ?)", (json.dumps(glm),))
            connection.execute("INSERT INTO session_v2 VALUES ('ses_b', ?)", (json.dumps(kimi),))
            connection.execute("INSERT INTO session_v2 VALUES ('ses_d', NULL)")
            connection.commit()
            connection.close()

            self.assertEqual(llm.session_models("ses_a", 95, 240, db_path=db),
                             {"kimi/zhipu/glm-5.4#high": 2})
            self.assertEqual(llm.session_models("ses_b", 95, 240, db_path=db), {"kimi/moonshot/kimi-k3.1-pro#default": 1})
            self.assertEqual(llm.session_models("ses_c", 95, 240, db_path=db), {})
            self.assertIsNone(llm.session_models(None, 95, 240, db_path=db))
            self.assertIsNone(llm.session_models("ses_a", 95, 240, db_path=root / "nowhere.db"))
            self.assertEqual(llm.session_model_index(db_path=db),
                             {"ses_a": "kimi/zhipu/glm-5.4#high", "ses_b": "kimi/moonshot/kimi-k3.1-pro#default"})

            family = core.model_family
            self.assertEqual(family({"kimi/zhipu/glm-5.4#high": 3}, "proposer"), "glm")
            self.assertEqual(family({"kimi/zhipu/glm-5.4#high": 3, "kimi/glm-5.3#high": 1}, "auditor"), "glm")
            self.assertEqual(family({"kimi/moonshot/kimi-k3.1-pro#default": 1}, "crossval"), "kimi")
            self.assertEqual(family({"kimi/kimi-k3#default": 1}, "crossval"), "kimi")
            self.assertEqual(family(None, "proposer"), "glm")
            self.assertEqual(family(None, "crossval"), "glm")   # the role's label, since the record could not be read

            state = core.initial_state("x")
            state["tokens"] = {"glm": dict(input=10, output=1, reasoning=1, calls=1, by_model={})}
            state["tasks"] = {"proposer_g1_A": dict(status="ok", session_id="ses_a", models={"kimi/zhipu/glm-5.4#high": 2}),
                              "crossval_x": dict(status="ok", session_id="ses_b"),
                              "proposer_g1_B": dict(status="ok", session_id="ses_z")}
            original = llm.OPENCODE_DB
            llm.OPENCODE_DB = db
            try:
                used, unverified = report.models_actually_used(state)
            finally:
                llm.OPENCODE_DB = original
            self.assertEqual(used, {"glm-5.4（high 档）": 1, "kimi-k3.1-pro": 1})
            self.assertEqual(unverified, 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class StoreTests(unittest.TestCase):
    def test_state_round_trip_and_ledger(self):
        root = Path(tempfile.mkdtemp())
        try:
            store_ = store.Store(root)
            state = core.initial_state("abc")
            store_.save(state)
            self.assertEqual(store_.load()["base_commit"], "abc")
            store_.append(dict(kind="a", value=1))
            store_.append(dict(kind="b", value=2))
            self.assertEqual([e["kind"] for e in store_.events()], ["a", "b"])
            self.assertEqual(len(store_.events("a")), 1)
            with self.assertRaises(TimeoutError):
                with store.Lock(root / "l.lock", timeout=0.1):
                    with store.Lock(root / "l.lock", timeout=0.1):
                        pass
        finally:
            shutil.rmtree(root, ignore_errors=True)


class DaemonTests(unittest.TestCase):
    def test_open_tier_follows_the_ladder(self):
        state = core.initial_state("x")
        for rung, tier in ((0, 0), (2, 0), (3, 1), (4, 2), (5, 3), (8, 3)):
            state["ladder"] = rung
            self.assertEqual(core.open_tier(state), tier, rung)

    def test_new_run_spec(self):
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            run_id = daemon.new_run("408", "light", "baseline")
            spec = json.loads((root / "runs" / run_id / "spec.json").read_text(encoding="utf-8"))
            remote = spec["remote"]
            self.assertEqual(remote["bench"], config.WORKLOADS["408"]["bench"])
            self.assertTrue(remote["export_dir"].startswith("/mnt/"))
            self.assertTrue(remote["base_manifest"].endswith("base_manifest.json"))
            self.assertEqual(spec["exclusive"], False)
            stress = daemon.new_run("bw10_540", "light", "stress_baseline", stress="bw10_540")
            remote = json.loads((root / "runs" / stress / "spec.json").read_text(encoding="utf-8"))["remote"]
            self.assertTrue(remote["input_dir"].endswith("/stress/bw10_540/input"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_initial_state_is_complete(self):
        state = core.initial_state("x")
        for key in ("phase", "baseline", "candidates", "gen", "meta", "map", "ladder", "crossval"):
            self.assertIn(key, state)
        self.assertEqual(state["phase"], "stage0")


class ReportTests(unittest.TestCase):
    def test_bounds_from_measured_baselines(self):
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            digest = dict(reference=dict(mean_ttft_s=0.8, makespan_s=9.0, compute_floor_s=9.0, link_floor_s=5.0))
            for workload in config.WORKLOADS:
                runs = []
                for ttft, makespan in ((2.0, 11.0), (2.1, 11.5)):
                    run_id = "b_%s_%d" % (workload, len(runs))
                    write_json(root / "runs" / run_id / "result.json", dict(status="done", digest=digest))
                    runs.append(run_id)
                daemon.state["baseline"][workload] = dict(light=dict(ttft=[2.0, 2.1], makespan=[11.0, 11.5], runs=runs))
            report.write_bounds(daemon)
            bounds = daemon.state["bounds"]["408"]
            self.assertAlmostEqual(bounds["light_makespan"], 11.25)
            self.assertAlmostEqual(bounds["light_ttft"], 2.05)
            self.assertAlmostEqual(bounds["no_contention_makespan"], 9.0)
            self.assertAlmostEqual(bounds["floor_makespan"], 9.0)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_generation_report_shows_where_each_candidate_helped_and_hurt(self):
        """The meta-strategist writes lessons from this report; one sentence per candidate is not enough."""
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            daemon.state["gen"] = dict(n=1, step="meta")
            daemon.state["meta"] = {}
            for workload in config.WORKLOADS:
                daemon.state["baseline"][workload] = dict(light=dict(ttft=[2.0] * 4, makespan=[10.0] * 4, runs=[]))
            daemon.state["operator_notes"] = [dict(at="2026-11-26 04:06:10", text="回放改为一次一轮")]
            runs = {workload: dict(screen=[dict(run="r_%s" % workload, ttft=2.0 * (1 - shift), makespan=10.0 * (1 + shift), stress=None)])
                    for workload, shift in (("408", 0.10), ("540", 0.10), ("mooncake", -0.20), ("agentx-narrow", 0.10), ("agentx-flex", 0.10))}
            card = dict(name="需求预算闸门 | 回填", mechanism=dict(signal=["demand_sum"], action=["admit", "hold"], timing=["arrival"],
                                                                  feedback=["open_loop"]),
                        params=dict(budget_factor=dict(value=1.15, min=1.0, max=2.0)), causal="在持需求之和超出链路", failure="链路没有富余时回退")
            daemon.state["candidates"]["c0001"] = dict(
                id="c0001", kind="llm", gen=1, status="rejected", card=card, runs=runs, score=-0.05, reason="筛选未通过")
            daemon.state["candidates"]["c0002"] = dict(id="c0002", kind="llm", gen=1, status="rejected", card=dict(name="x"), runs={}, reason="门禁未过")
            # The ledger says replays shared the host only while r_408 was started.
            with (root / "ledger.jsonl").open("a", encoding="utf-8") as stream:
                for event in (dict(ts=50, kind="manual_parallel", parallel=2), dict(ts=60, kind="run_started", run="r_408"),
                              dict(ts=70, kind="manual_parallel", parallel=1), dict(ts=80, kind="run_started", run="r_mooncake")):
                    stream.write(json.dumps(event) + "\n")
            path = report.write_generation_report(daemon)
            text = (root / path).read_text(encoding="utf-8")
            self.assertIn("## 本代候选的逐负载结果", text)
            self.assertIn("| mooncake | -20.0% | +20.0% | 1 | 1 | 是 |", text)
            self.assertIn("| 408 | +10.0% | -10.0% | 1 | 2 | 是 |", text)
            # The mechanism, the parameters and the proposer's own reasoning, so that nothing is inferred from a name.
            self.assertIn("demand_sum × admit/hold · arrival · open_loop | budget_factor=1.15 |", text)
            self.assertIn("需求预算闸门 \\| 回填", text)
            self.assertIn("原因链：在持需求之和超出链路", text)
            self.assertIn("预设的失败形态：链路没有富余时回退", text)
            self.assertIn("## 评价规则（冻结，不能修改；写在这里，免得靠猜）", text)
            self.assertIn("排序分 = 0.5 ×（各负载得分的平均值）+ 0.5 ×（最差负载的得分）", text)
            # A candidate without a card is left out of the per-workload and self-description sections.
            self.assertEqual(text.count("### c0002（x）"), 1)
            # A pipe inside a name must not split the row into extra columns.
            self.assertEqual(report.table(["候选", "机制"], [["c0003", "窗口 | 豁免"]]),
                             "| 候选 | 机制 |\n|---|---|\n| c0003 | 窗口 \\| 豁免 |")
            self.assertIn("- 2026-11-26 04:06:10：回放改为一次一轮", text)
            # The proposers' context carries the same note.
            report.write_context(daemon, 1, "A")
            self.assertIn("回放改为一次一轮", (root / "digest" / "gen_1" / "context_A.md").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_dashboard_renders_from_an_empty_state(self):
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            report.Dashboard(daemon).render()
            page = (root / "dashboard" / "index.html").read_text(encoding="utf-8")
            self.assertIn("TFS 自动调优看板", page)
            self.assertIn("机制地图", page)
            # A page that stops being redrawn must say so itself: the daemon may be down.
            self.assertNotIn("RENDERED_MS", page)
            self.assertRegex(page, r"Date\.now\(\) - \d{13}\)")
            self.assertRegex(page, r"更新于 \d\d:\d\d:\d\d")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class SignatureVocabulary(unittest.TestCase):
    def test_card_vocabulary_matches_the_digest_paths(self):
        # A metric the card may promise must be something the gate can look up, and vice versa.
        # The two client-side metrics come from the run summary instead of the digest.
        client_side = {"mean_ttft_s", "makespan_s"}
        self.assertEqual(set(config.SIGNATURE_METRICS) - client_side, set(gates.METRIC_PATHS))
        self.assertTrue(client_side <= set(config.SIGNATURE_METRICS))

    def test_every_promisable_metric_exists_in_the_digest_code(self):
        """``wsl/digest.py`` returns these keys; a renamed key would silently fail every promise."""
        source = (HERE / "wsl" / "digest.py").read_text(encoding="utf-8")
        for metric, (section, key) in gates.METRIC_PATHS.items():
            if metric in ("mean_ttft_s", "makespan_s"):
                continue  # client-side metrics, read from the summary
            if key is None:
                self.assertRegex(source, r"\b%s=" % re.escape(section), metric)  # a top-level scalar
                continue
            self.assertRegex(source, r"%s=dict\(" % re.escape(section), metric)
            self.assertRegex(source, r"\b%s=" % re.escape(key), metric)

    def test_a_wrongly_named_promise_is_refused_by_the_card_check(self):
        card = dict(name="x", tier=0, mechanism=dict(signal=["demand_sum"], action=["admit"], timing=["schedule"], feedback=["closed_loop"]),
                   params={}, causal="因果链说明文字需要足够长才能通过检查，这里是凑长度的说明文字。",
                   signature=[dict(workload="408", metric="overload.read_stretch_p95", direction="down")],
                   failure="失效模式的说明文字也需要足够长才能通过检查，这里是凑长度的说明文字。",
                   protection="保护机制的说明文字也需要足够长才能通过检查，这里是凑长度的说明文字。")
        self.assertTrue(any("signature" in issue for issue in gates.check_card(card, 0).issues))
        card["signature"] = [dict(workload="408", metric="read_stretch_p95", direction="down")]
        self.assertEqual(gates.check_card(card, 0).issues, [])


if __name__ == "__main__":
    unittest.main()
