"""Stage 0 must survive a state written by the earlier version that reused one flag for two phases."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from tuning import daemon as core  # noqa: E402


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
            daemon.new_run = lambda *args, **kwargs: queued.append(args) or "r%d" % len(queued)
            daemon.stage0()
            self.assertEqual(state["stage0"]["armed"], True)
            self.assertEqual(state["stage0"]["level"], 2)
            self.assertEqual(len(queued), 4)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
