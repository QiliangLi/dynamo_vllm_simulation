import asyncio
import inspect
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sim.stack.best_effort import RecoveringControlClient
from sim.stack.async_score import AsyncScoreClient
from sim.stack.scheduler_admission import ReadGate


class Client:
    def __init__(self, response=None, fail=False):
        self.response = response
        self.fail = fail
        self.calls = 0
        self.closed = False

    def check(self):
        if self.closed:
            raise ConnectionError('closed')

    def control(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise TimeoutError('ack lost')
        return self.response

    def close(self):
        self.closed = True


def request(rid='test'):
    hint = dict(version=1, is_heavy_read=True, estimated_remote_read_bytes=4096,
                threshold_version=1)
    return SimpleNamespace(request_id=rid,
                           sampling_params=SimpleNamespace(extra_args={'storage_hint': hint}))


class WorkerTests(unittest.TestCase):
    def test_timeout_allows_without_fake_permit_and_does_not_replay_acquire(self):
        first = Client(fail=True)
        second = Client(dict(granted=True, permit_id='new-permit'))
        clients = iter([first, second])
        wrapper = RecoveringControlClient(lambda: next(clients), retry_s=0)
        gate = ReadGate(wrapper)
        try:
            self.assertTrue(gate.allow(request()))
            self.assertNotIn('test', gate.permits)
            self.assertEqual(first.calls, 1)
            deadline = time.monotonic() + 2
            while wrapper.client is not second and time.monotonic() < deadline:
                wrapper.check()
                time.sleep(.001)
            self.assertIs(wrapper.client, second)
            # The ambiguous old request stays bypassed after recovery.
            self.assertTrue(gate.allow(request()))
            self.assertEqual(second.calls, 0)
            self.assertTrue(gate.allow(request('new')))
            self.assertEqual(gate.permits['new'], 'new-permit')
        finally:
            wrapper.close()

    def test_real_full_reply_still_waits(self):
        client = Client(dict(granted=False, reason='full', permit_id=None))
        wrapper = RecoveringControlClient(lambda: client)
        try:
            gate = ReadGate(wrapper)
            self.assertFalse(gate.allow(request()))
            self.assertFalse(gate.bypassed)
        finally:
            wrapper.close()

    def test_rollback_failure_removes_local_permit_without_reacquiring(self):
        client = Client(dict(granted=True, permit_id='p'))
        wrapper = RecoveringControlClient(lambda: client)
        gate = ReadGate(wrapper)
        try:
            r = request()
            self.assertTrue(gate.allow(r))
            client.fail = True
            gate.rollback(r)
            self.assertFalse(gate.permits)
            self.assertTrue(gate.allow(r))
            self.assertEqual(client.calls, 2)
        finally:
            wrapper.close()

    def test_late_local_connection_failure_does_not_kill_begin_step(self):
        client = Client()
        wrapper = RecoveringControlClient(lambda: client)
        try:
            client.closed = True
            ReadGate(wrapper).begin_step()
            self.assertIsNone(wrapper.client)
        finally:
            wrapper.close()


class ScoreTests(unittest.TestCase):
    def test_slow_score_server_does_not_block_central_and_updates_stay_ordered(self):
        release = threading.Event()
        entered = threading.Event()
        operations = []

        class SlowScore:
            def __init__(self, *args): pass
            def exchange(self, op, rid=None, **fields):
                operations.append(op)
                if op == 'activate':
                    entered.set()
                    release.wait(2)
                return dict(duration_wall_ns=1)
            def close(self): pass

        with patch('sim.stack.async_score.ScoreClient', SlowScore):
            score = AsyncScoreClient('unused', .1, .1)
            try:
                score.exchange('activate', 'id', permit_id='p')
                self.assertTrue(entered.wait(1))
                reply = score.exchange('rollback', 'id', permit_id='p')
                self.assertTrue(reply['queued'])
                self.assertNotIn('rollback', operations)
                release.set()
                deadline = time.monotonic() + 1
                while 'rollback' not in operations and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertLess(operations.index('activate'), operations.index('rollback'))
            finally:
                release.set()
                score.close()
                score.thread.join(2)
