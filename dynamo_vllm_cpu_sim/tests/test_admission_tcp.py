"""Real TCP sockets: global ledger contention, framing, clocks and failures."""
import asyncio
import json
from pathlib import Path
import socket
import tempfile
import unittest

from sim.stack.admission_rpc import clock_id, parse_endpoint, tcp_endpoint
from sim.stack.admission_service import create_app
from sim.stack.feedback import FeedbackClient
import test_stack_admission as base_tests


class AddressTests(unittest.TestCase):
    def test_tcp_and_legacy_endpoints(self):
        self.assertEqual(parse_endpoint('tcp://127.0.0.1:1234'),('tcp',('127.0.0.1',1234)))
        self.assertEqual(parse_endpoint('tcp://[::1]:1234'),('tcp',('::1',1234)))
        self.assertEqual(parse_endpoint('unix:///tmp/control.sock'),('unix','/tmp/control.sock'))
        self.assertEqual(parse_endpoint('/tmp/control.sock'),('unix','/tmp/control.sock'))
        self.assertEqual(parse_endpoint('tcp://127.0.0.1:0',server=True)[1][1],0)
        for value in ('tcp://127.0.0.1:0','tcp://host','tcp://host:65536',
                      'tcp://user@host:1234','tcp://host:1234/a','http://host:80','relative.sock'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_endpoint(value)


class TCPAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.ready=Path(self.tmp.name)/'ready.json'
        config=base_tests.config()
        config['scheduler']='upstream'
        config['admission'].update(placement='scheduler',classification='arrival')
        self.app=create_app(config,'http://127.0.0.1:1',feedback_endpoint='tcp://127.0.0.1:0',
                            feedback_ready_file=self.ready)
        self.lifespan=self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.state=self.app.state.admission
        self.endpoint=json.loads(self.ready.read_text())['endpoint']
        self.clients=[FeedbackClient(self.endpoint,w) for w in range(2)]
        for rid in ('a','b'):
            self.state.arrive(dict(id=rid,prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
        self.state.admit()

    async def asyncTearDown(self):
        for client in self.clients:
            client.close()
        await self.lifespan.__aexit__(None,None,None)
        self.tmp.cleanup()

    async def acquire(self,rid,worker,key='first'):
        return await asyncio.to_thread(self.clients[worker].control,'acquire',
            self.state.uuid_by_id[rid]+'_0',acquisition_key=key,load_attempt_id=1,
            storage_hint=self.state.controller.hints[rid])

    async def test_concurrent_workers_share_one_slot_and_release_on_first_token(self):
        responses=await asyncio.gather(self.acquire('a',0),self.acquire('b',1))
        self.assertEqual(sum(r['granted'] for r in responses),1)
        self.assertTrue(all(r['rpc_transport']=='tcp' for r in responses))
        winner=next(i for i,r in enumerate(responses) if r['granted'])
        loser=1-winner
        rid=('a','b')[winner]
        token=responses[winner]['permit_id']
        self.assertEqual((await self.acquire(rid,winner))['permit_id'],token)
        client=self.clients[winner]
        identity=self.state.uuid_by_id[rid]
        self.assertEqual(client.sock.family,socket.AF_INET)
        self.assertNotEqual(client.sock.getsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY),0)
        self.assertFalse((await self.acquire(('a','b')[loser],loser))['granted'])
        self.state.local_event('first_token',rid)
        self.assertTrue((await self.acquire(('a','b')[loser],loser))['granted'])
        self.assertEqual(self.state.controller.permits[token]['state'],'released')
        events=[e for e in self.state.events if e['event']=='feedback_received' and e.get('engine_id')]
        self.assertEqual(events,[])

    async def test_rollback_is_idempotent_over_persistent_connection(self):
        first=await self.acquire('a',0)
        for _ in range(2):
            reply=await asyncio.to_thread(self.clients[0].control,'rollback',self.state.uuid_by_id['a'],
                                           permit_id=first['permit_id'])
            self.assertTrue(reply['released'])
        self.assertEqual(self.state.controller.held,0)
        self.assertFalse((await self.acquire('a',0))['granted'])
        self.assertTrue((await self.acquire('a',0,'retry'))['granted'])

    async def test_removed_feedback_is_rejected_over_tcp(self):
        await self.acquire('a',0)
        with self.assertRaisesRegex(RuntimeError, 'removed'):
            await asyncio.to_thread(self.clients[0]._exchange,dict(
            event_id='other-host:1',kind='load_submitted',request_id=self.state.uuid_by_id['a'],
            worker_id=0,clock_id='different-machine',occurred_mono=1e12,
            payload=dict(attempt_id=1,submitted_read_bytes=1024)))
        self.assertTrue(self.clients[0].closed)
        self.assertEqual(self.state.controller.held,1)

    async def test_fragmented_and_coalesced_request_frames(self):
        host,port=parse_endpoint(self.endpoint)[1]
        reader,writer=await asyncio.open_connection(host,port)
        try:
            wire=b'{"op":"ping"}\n'
            writer.write(wire[:5]);await writer.drain();await asyncio.sleep(.001)
            writer.write(wire[5:]+wire);await writer.drain()
            for _ in range(2):
                self.assertEqual(json.loads(await reader.readline())['rpc_transport'],'tcp')
        finally:
            writer.close();await writer.wait_closed()


class TCPFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_response_is_reassembled(self):
        async def handler(reader,writer):
            await reader.readline()
            writer.write(b'{"ok":true,');await writer.drain();await asyncio.sleep(.001)
            writer.write(b'"rpc_transport":"tcp"}\n');await writer.drain()
            writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        client=FeedbackClient(tcp_endpoint(*server.sockets[0].getsockname()[:2]),0)
        try:
            self.assertTrue((await asyncio.to_thread(client.control,'ping','probe'))['ok'])
        finally:
            client.close();server.close();await server.wait_closed()

    async def test_disconnect_after_request_never_replays_or_falls_back(self):
        received=[]
        async def handler(reader,writer):
            received.append(await reader.readline())
            writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        client=FeedbackClient(tcp_endpoint(*server.sockets[0].getsockname()[:2]),0)
        try:
            with self.assertRaises(ConnectionError):
                await asyncio.to_thread(client.control,'acquire','probe')
            self.assertTrue(client.closed)
            with self.assertRaises(ConnectionError):
                await asyncio.to_thread(client.control,'acquire','probe')
            self.assertEqual(len(received),1)
        finally:
            client.close();server.close();await server.wait_closed()

    async def test_timeout_fails_closed(self):
        finished=asyncio.Event()
        async def handler(reader,writer):
            try:
                await reader.readline()
                await reader.read()  # No response; wait for the timed-out client to close.
            finally:
                writer.close();await writer.wait_closed();finished.set()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        client=FeedbackClient(tcp_endpoint(*server.sockets[0].getsockname()[:2]),0,timeout_s=.03)
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.to_thread(client.control,'acquire','probe')
            self.assertTrue(client.closed)
            await asyncio.wait_for(finished.wait(),1)
        finally:
            client.close();server.close();await server.wait_closed()


if __name__=='__main__':
    unittest.main()
