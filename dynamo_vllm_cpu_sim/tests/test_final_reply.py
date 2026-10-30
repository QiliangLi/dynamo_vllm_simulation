"""Final acknowledgements, ordering, bounded tails and late protocol failures."""
import asyncio
from types import SimpleNamespace
import unittest

from sim.stack.dynamo_admission_rpc import PROTOCOL, envelope, validate_reply
from sim.stack.final_reply import FinalReplyStreams


def valid(request):
    return dict(ok=True, rpc_final=True, rpc_protocol=PROTOCOL,
                rpc_call_id=request['call_id'], rpc_transport='dynamo',
                rpc_request_plane='tcp', rpc_codec='msgpack', rpc_instance_id=7,
                rpc_ledger_epoch='epoch', granted=True, permit_id='permit')


class FinalReplyTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, streams, generator, op='acquire', changes=None):
        request=envelope(dict(op=op), 'epoch')
        async def direct(received, instance):
            self.assertEqual(received, request);self.assertEqual(instance, 7)
            async def frames():
                async for response in generator(dict(valid(request), **(changes or {}))):
                    yield SimpleNamespace(data=lambda response=response:response)
            return frames()
        return await streams.request(SimpleNamespace(direct=direct), 7, request,
            lambda reply:validate_reply(reply, request, 7), {})

    async def test_final_reply_and_next_operation_do_not_wait_for_eof(self):
        release=asyncio.Event();ended=[]
        async def generator(response):
            yield response
            await release.wait()
            ended.append(response['rpc_call_id'])
        streams=FinalReplyStreams(1)
        first=await self.call(streams,generator)
        second=await self.call(streams,generator,op='rollback')
        self.assertEqual(ended,[])
        self.assertEqual(streams.snapshot()['pending_streams'],2)
        self.assertNotEqual(first['rpc_call_id'],second['rpc_call_id'])
        release.set();stats=await streams.settle()
        self.assertEqual(stats['final_replies'],stats['drained_streams'])
        self.assertEqual(stats['pending_streams'],0)

    async def test_corrupt_or_nonfinal_ack_never_releases_caller(self):
        async def generator(response):yield response
        for change in (dict(rpc_final=False),dict(rpc_call_id='other'),dict(rpc_ledger_epoch='other'),
                       dict(rpc_instance_id=8),dict(rpc_protocol='dynamo-admission-v1'),dict(ok=False)):
            with self.subTest(change=change):
                streams=FinalReplyStreams(1)
                with self.assertRaises(RuntimeError):await self.call(streams,generator,changes=change)
                self.assertEqual(streams.replies,0)

    async def test_empty_response_never_releases_caller(self):
        async def generator(response):
            if False:yield response
        with self.assertRaises(ConnectionError):await self.call(FinalReplyStreams(1),generator)

    async def test_extra_data_after_ack_fails_future_admission_and_audit(self):
        release=asyncio.Event()
        async def generator(response):
            yield response
            await release.wait()
            yield response
        streams=FinalReplyStreams(1)
        self.assertTrue((await self.call(streams,generator))['granted'])
        release.set()
        with self.assertRaises(RuntimeError):await streams.settle()
        with self.assertRaises(RuntimeError):await self.call(streams,generator)
        self.assertEqual(streams.replies,1)  # No replay or second mutation.
        self.assertEqual(streams.failed,1)
        self.assertEqual(len(streams.pending),0)

    async def test_late_transport_error_is_observed(self):
        async def generator(response):
            yield response
            raise ConnectionError('late transport failure')
        streams=FinalReplyStreams(1)
        await self.call(streams,generator)
        with self.assertRaises(RuntimeError):await streams.settle()
        self.assertIsInstance(streams.failure,ConnectionError)

    async def test_missing_eof_has_a_separate_bounded_timeout(self):
        async def generator(response):
            yield response
            await asyncio.Event().wait()
        streams=FinalReplyStreams(.02)
        self.assertTrue((await self.call(streams,generator))['granted'])
        with self.assertRaises(RuntimeError):await streams.settle()
        self.assertIsInstance(streams.failure,TimeoutError)
        self.assertEqual(len(streams.pending),0)

    async def test_capacity_stops_before_sending_another_mutation(self):
        release=asyncio.Event();sent=[]
        async def generator(response):
            sent.append(response['rpc_call_id'])
            yield response
            await release.wait()
        streams=FinalReplyStreams(1,max_pending=2)
        await self.call(streams,generator);await self.call(streams,generator)
        with self.assertRaisesRegex(RuntimeError,'capacity'):await self.call(streams,generator)
        self.assertEqual(len(sent),2)
        self.assertEqual(streams.peak_pending,2)
        release.set()
        with self.assertRaises(RuntimeError):await streams.settle()
        self.assertEqual(len(streams.pending),0)

    async def test_concurrent_calls_serialize_through_ack_only(self):
        first_reply=asyncio.Event();tails=asyncio.Event();entered=[]
        class Client:
            async def direct(self,request,instance):
                entered.append(request['body']['op'])
                async def stream():
                    if len(entered)==1:await first_reply.wait()
                    yield SimpleNamespace(data=lambda:valid(request))
                    await tails.wait()
                return stream()
        streams=FinalReplyStreams(1);client=Client()
        async def call(op):
            req=envelope(dict(op=op),'epoch')
            return await streams.request(client,7,req,lambda r:validate_reply(r,req,7),{})
        a=asyncio.create_task(call('acquire'));b=asyncio.create_task(call('rollback'))
        await asyncio.sleep(0)
        self.assertEqual(entered,['acquire'])
        first_reply.set();await asyncio.gather(a,b)
        self.assertEqual(entered,['acquire','rollback'])
        self.assertEqual(streams.snapshot()['pending_streams'],2)
        tails.set();await streams.settle()
