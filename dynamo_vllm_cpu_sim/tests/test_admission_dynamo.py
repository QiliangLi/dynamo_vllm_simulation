"""Native TCP/MessagePack in real separate processes, including sync bridge."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

from sim.stack.admission_rpc import parse_endpoint


class NativeAddressTests(unittest.TestCase):
    def test_native_endpoint_name(self):
        self.assertEqual(parse_endpoint('dyn://tfs-test.ledger.control'),
                         ('dynamo','tfs-test.ledger.control'))
        for endpoint in ('dyn://ledger','dyn://a.b.c.d','dyn://a.b.c/','dyn://a.b.c?x=1'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                parse_endpoint(endpoint)


class NativeProcessTests(unittest.TestCase):
    def run_case(self, mode):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root/'tests/native_admission_fixture.py'), mode],
                                cwd=root, env=dict(os.environ, PYTHONPATH=str(root)),
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout[-1000:]+result.stderr[-5000:])
        self.assertIn('"status": "pass"', result.stdout)

    def test_atomic_ledger_replay_and_release_without_execution_feedback(self):
        self.run_case('ledger')

    def test_same_worker_forces_overlap_in_ledger_without_release_polling(self):
        self.run_case('force')

    def test_pinned_ledger_rejects_wrong_instance_and_epoch(self):
        self.run_case('identity')

    def test_ambiguous_timeout_keeps_ledger_and_closes_client_without_replay(self):
        self.run_case('timeout')

    def test_next_outbox_operation_commits_while_previous_streams_remain_open(self):
        self.run_case('tail')

    def test_extra_response_fails_after_ack_without_replaying_the_permit(self):
        self.run_case('tail-extra')

    def test_missing_stream_end_fails_after_ack_without_replaying_the_permit(self):
        self.run_case('tail-timeout')
