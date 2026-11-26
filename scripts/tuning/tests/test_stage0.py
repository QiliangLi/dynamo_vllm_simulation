"""Stage 0 must survive a state written by the earlier version that reused one flag for two phases."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from tuning import daemon as core  # noqa: E402
from tuning.store import read_json, write_json  # noqa: E402


def phase(body, start, end):
    return body[body.index(start):body.index(end)]


class Stage0Flags(unittest.TestCase):
    def test_phases_do_not_share_a_flag(self):
        source = (HERE / "tuning" / "daemon.py").read_text(encoding="utf-8")
        body = source[source.index("    def stage0(self):"):source.index("    def decide_parallel(self):")]
        baseline = phase(body, 'if step["step"] == "baseline":', 'if step["step"] == "parallel":')
        parallel = phase(body, 'if step["step"] == "parallel":', 'if step["step"] == "stress":')
        stress = phase(body, 'if step["step"] == "stress":', 'if step["step"] == "bounds":')
        self.assertIn('step["queued"] = True', baseline)
        self.assertNotIn('step["armed"] = True', baseline)
        self.assertIn("step.update(armed=True, level=level)", parallel)
        self.assertIn("built=True", stress)
        self.assertNotIn('step["armed"] = True', stress)

    def test_a_stale_armed_flag_is_repaired(self):
        root = Path(tempfile.mkdtemp())
        try:
            state = core.initial_state("x")
            # What the earlier version left behind when the baseline phase handed over.
            state["stage0"] = dict(step="parallel", level_tested=1, armed=True)
            daemon = core.Daemon(root)
            daemon.state = state
            queued = []

            def fake_new_run(*args, **kwargs):
                queued.append((args, kwargs))
                return "r%d" % len(queued)
            daemon.new_run = fake_new_run
            daemon.stage0()
            self.assertEqual(state["stage0"]["armed"], True)
            self.assertEqual(state["stage0"]["level"], 2)
            # Two groups, every core workload in each, two replays per group.
            self.assertEqual(len(queued), 20)
            self.assertEqual(len({kwargs["group"] for args, kwargs in queued}), 10)
            self.assertEqual({args[0] for args, kwargs in queued}, set(core.config.WORKLOADS))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ParallelLevel(unittest.TestCase):
    def decide(self, shifts):
        """shifts: {level: {workload: relative shift of the probe results}}; returns the chosen level."""
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            state = core.initial_state("x")
            state["baseline"] = {
                workload: {"light": {"ttft": [10.0] * 7, "makespan": [40.0] * 7, "runs": []}}
                for workload in core.config.WORKLOADS}
            daemon.state = state
            count = 0
            for level, workloads in shifts.items():
                for workload, shift in workloads.items():
                    for index in range(4):
                        count += 1
                        run_id = "r%04d" % count
                        spec = dict(run_id=run_id, workload=workload, arm="light", tag="probe", cand=None,
                                    group="probe%d_0_%s" % (level, workload))
                        summary = dict(mean_ttft_s=10.0 * (1 + shift), makespan_s=40.0 * (1 + shift))
                        write_json(root / "runs" / run_id / "spec.json", spec)
                        write_json(root / "runs" / run_id / "result.json", dict(status="done", summary=summary))
            daemon.decide_parallel()
            self.assertEqual(state["stage0"]["step"], "stress")
            return state["parallel"]
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_one_sensitive_workload_refuses_the_level(self):
        """408 read 7.7% slower in TTFT at level 2 while the others did not move."""
        level = self.decide({2: {"408": 0.077, "540": 0.0, "mooncake": 0.0, "agentx-narrow": 0.0, "agentx-flex": 0.0}})
        self.assertEqual(level, 1)

    def test_a_level_nobody_reacts_to_is_taken(self):
        self.assertEqual(self.decide({2: {w: 0.0 for w in core.config.WORKLOADS}}), 2)
        self.assertEqual(self.decide({2: {w: 0.0 for w in core.config.WORKLOADS}, 3: {w: 0.0 for w in core.config.WORKLOADS}}), 3)

    def test_a_level_above_a_refused_one_is_not_taken(self):
        self.assertEqual(self.decide({2: {"408": 0.077, "540": 0.0, "mooncake": 0.0, "agentx-narrow": 0.0, "agentx-flex": 0.0},
                                      3: {w: 0.0 for w in core.config.WORKLOADS}}), 1)

    def test_a_workload_without_probes_refuses_the_level(self):
        self.assertEqual(self.decide({2: {w: 0.0 for w in list(core.config.WORKLOADS)[:4]}}), 1)


class CandidateCollection(unittest.TestCase):
    def test_collecting_a_candidate_replay_keeps_the_state_writable(self):
        """The first real candidate replay crashed the daemon: a counter was keyed by a tuple."""
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            daemon.state["phase"] = "run"
            daemon.state["candidates"]["c0001"] = dict(id="c0001", kind="llm", gen=1, status="smoke", runs={}, queued={}, fail={})
            run_id = daemon.new_run("408", "candidate", "smoke", cand="c0001", gen=1)
            daemon.state["queue"].remove(run_id)
            summary = dict(requests=408, workers=4, heavy=0, makespan_s=12.0, worker_finish_s=[12.0] * 4,
                           mean_worker_finish_s=12.0, mean_ttft_s=2.0, median_ttft_s=2.0, p95_ttft_s=3.0,
                           p99_ttft_s=3.5, max_ttft_s=4.0)
            write_json(root / "runs" / run_id / "result.json", dict(status="done", summary=summary, digest={}))
            daemon.collect(run_id, read_json(root / "runs" / run_id / "result.json"))
            text = json.dumps(daemon.state)
            self.assertIn('"smoke|408"', text)
            self.assertEqual(len(daemon.state["candidates"]["c0001"]["runs"]["408"]["smoke"]), 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def test_a_stopped_candidate_says_what_broke(self):
        """The runner only says ``replay exited with 1``; the exception is the last line of its log."""
        root = Path(tempfile.mkdtemp())
        try:
            daemon = core.Daemon(root)
            daemon.state = core.initial_state("x")
            daemon.state["phase"] = "run"
            daemon.state["candidates"]["c0002"] = dict(id="c0002", kind="llm", gen=1, status="screen", runs={}, queued={}, fail={})
            failed = dict(status="failed", error="RunError: replay exited with 1", summary=None,
                          log_tail="replayed 540: ok=540\nTraceback (most recent call last):\n  File \"/root/tuning/runs/r00066/harness/sim/stack/load_observation.py\", line 85, in with_local_observations\n    rid = aliases[uuid.UUID(match.group()).hex]\nKeyError: '9ee0acf28ccf4bae9dfc651ecb15bc94'\n")
            for _ in range(2):
                run_id = daemon.new_run("540", "candidate", "screen", cand="c0002", gen=1)
                daemon.state["queue"].remove(run_id)
                daemon.collect(run_id, failed)
            # The replacement asked for by the first failure is still queued.
            self.assertEqual(len(daemon.state["queue"]), 1)
            cand = daemon.state["candidates"]["c0002"]
            self.assertIn("540 screen：RunError: replay exited with 1 | load_observation.py:85 with_local_observations: "
                          "KeyError: '9ee0acf28ccf4bae9dfc651ecb15bc94'", cand["run_failed"])
            json.dumps(daemon.state)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
