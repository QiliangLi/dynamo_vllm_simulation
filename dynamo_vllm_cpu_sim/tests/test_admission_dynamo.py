"""Native TCP/MessagePack in real separate processes, including sync bridge."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sim.stack import dynamo_admission_rpc
from sim.stack.admission_rpc import parse_endpoint


class NativeAddressTests(unittest.TestCase):
    def test_native_endpoint_name(self):
        self.assertEqual(parse_endpoint('dyn://tfs-test.ledger.control'),
                         ('dynamo','tfs-test.ledger.control'))
        for endpoint in ('dyn://ledger','dyn://a.b.c.d','dyn://a.b.c/','dyn://a.b.c?x=1'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                parse_endpoint(endpoint)


class StartupOrderTests(unittest.IsolatedAsyncioTestCase):
    """The file store looks a bucket up and inserts it without one lock. A registration
    that looks it up while the client's watcher is still inserting loses the bucket,
    is never renewed, and other processes expire its record after 10 s."""

    async def test_watcher_creates_the_bucket_before_registration_looks_it_up(self):
        server = dynamo_admission_rpc.DynamoAdmissionServer('tfs-test.ledger.control', lambda body: {}, 1)

        class Endpoint:
            def __init__(self):
                self.bucket = None
                self.checking = 0
                self.races = 0
                self.calls = []

            async def get_or_create_bucket(self):
                self.calls.append('check')
                if self.bucket is not None:
                    return
                self.checking += 1
                await asyncio.sleep(0)  # lookup and insert are not one critical section
                self.races += self.checking > 1
                self.checking -= 1
                self.bucket = object()

            def connection_id(self):
                return 7

            def serve_endpoint(self, handler, graceful_shutdown=True):
                self.calls.append('serve')
                async def serve():
                    await self.get_or_create_bucket()  # discovery.register
                    await asyncio.Event().wait()
                return asyncio.ensure_future(serve())

            async def client(self):
                self.calls.append('client')
                asyncio.ensure_future(self.get_or_create_bucket())  # Manager::watch task
                async def nothing():
                    pass
                async def direct(request, instance_id):
                    async def frames():
                        yield SimpleNamespace(data=lambda: dict(
                            ok=True, rpc_final=True, rpc_protocol=dynamo_admission_rpc.PROTOCOL,
                            rpc_call_id=request['call_id'], rpc_transport='dynamo',
                            rpc_request_plane='tcp', rpc_codec='msgpack', rpc_instance_id=7,
                            rpc_ledger_epoch=server.epoch))
                    return frames()
                return SimpleNamespace(wait_for_instances=nothing, instance_ids=lambda: [7], direct=direct)

        endpoint = Endpoint()
        runtime = SimpleNamespace(endpoint=lambda name: endpoint, shutdown=lambda: None)
        with patch.object(dynamo_admission_rpc, 'new_runtime', lambda loop: runtime):
            info = await server.start()
        self.assertEqual(endpoint.calls[:2], ['client', 'check'])
        self.assertEqual(endpoint.races, 0)
        self.assertEqual(info['instance_id'], 7)
        await server.close()


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
