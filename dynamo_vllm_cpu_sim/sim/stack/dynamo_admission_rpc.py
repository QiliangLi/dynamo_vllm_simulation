"""Native Dynamo TCP/MessagePack admission endpoint and synchronous bridge.

The ledger handler runs on the server's existing asyncio loop. Each EngineCore
owns one background loop/thread and one exact-target Runtime client. Validated
final replies unblock callers; EOF checks continue in bounded background tasks.
Failed or ambiguous calls stop further admission, without fallback or replay.
"""
import asyncio
from concurrent.futures import Future
import os
import threading
import uuid

from sim.stack.final_reply import FinalReplyStreams

PROTOCOL = 'dynamo-admission-v2'


def envelope(body, epoch):
    return dict(protocol=PROTOCOL, ledger_epoch=epoch, call_id=uuid.uuid4().hex, body=body)


def validate_reply(response, request, instance_id, *, handshake=False):
    if (not isinstance(response, dict) or response.get('ok') is not True
            or response.get('rpc_final') is not True
            or response.get('rpc_protocol') != PROTOCOL
            or response.get('rpc_call_id') != request['call_id']
            or response.get('rpc_transport') != 'dynamo'
            or response.get('rpc_request_plane') != 'tcp'
            or response.get('rpc_codec') != 'msgpack'
            or response.get('rpc_instance_id') != instance_id
            or not isinstance(response.get('rpc_ledger_epoch'), str)
            or not response['rpc_ledger_epoch']
            or (not handshake and response['rpc_ledger_epoch'] != request['ledger_epoch'])):
        raise RuntimeError(f'admission RPC identity or final reply rejected: {response}')


def new_runtime(loop, *, client=False):
    # Import only in the process that owns the Runtime, after EngineCore spawn.
    from dynamo.runtime import DistributedRuntime
    if client:
        # EngineCore inherits its worker's system port. Its own Runtime must
        # not bind the already-running worker's health/metrics listener.
        os.environ['DYN_SYSTEM_PORT'] = '-1'
    if os.environ.get('DYN_REQUEST_PLANE_CODEC', 'msgpack').lower() != 'msgpack':
        raise ValueError('native admission requires DYN_REQUEST_PLANE_CODEC=msgpack')
    return DistributedRuntime(loop, os.environ.get('DYN_DISCOVERY_BACKEND', 'etcd'),
                              'tcp', event_plane='zmq')


async def one_reply(client, instance_id, request, phase=None):
    # direct() denies transport fallback in the pinned Dynamo version.
    if phase is not None:phase['stage']='await_direct'
    stream = await client.direct(request, instance_id)
    if phase is not None:phase['stage']='await_first_response'
    result = None
    count = 0
    async for item in stream:
        count += 1
        if count > 1:
            raise RuntimeError('admission RPC returned multiple responses')
        result = item.data()
        if phase is not None:phase['stage']='await_end_of_stream'
    if count != 1 or not isinstance(result, dict):
        raise ConnectionError('admission RPC requires exactly one object response')
    if phase is not None:phase['stage']='complete'
    return result


class DynamoAdmissionServer:
    def __init__(self, name, handler, timeout_s):
        self.name, self.handler, self.timeout_s = name, handler, timeout_s
        self.epoch = uuid.uuid4().hex
        self.runtime = None
        self.task = None

    async def start(self):
        self.runtime = new_runtime(asyncio.get_running_loop())
        endpoint = self.runtime.endpoint(self.name)
        self.instance_id = endpoint.connection_id()
        # The PyO3 binding returns an asyncio Future, rather than a coroutine.
        self.task = asyncio.ensure_future(endpoint.serve_endpoint(self.receive, graceful_shutdown=False))
        try:
            async with asyncio.timeout(self.timeout_s):
                client = await endpoint.client()
                await client.wait_for_instances()
                if set(client.instance_ids()) != {self.instance_id}:
                    raise RuntimeError('admission endpoint must have exactly one ledger instance')
                request = self.envelope({'op': 'ping'})
                result = await one_reply(client, self.instance_id, request)
                validate_reply(result, request, self.instance_id)
                if result.get('rpc_ledger_epoch') != self.epoch or result.get('ok') is not True:
                    raise RuntimeError('admission endpoint startup handshake failed')
        except BaseException:
            await self.close()
            raise
        return dict(endpoint='dyn://'+self.name, transport='dynamo', protocol=PROTOCOL,
                    request_plane='tcp', codec='msgpack', instance_id=self.instance_id,
                    ledger_epoch=self.epoch, server_pid=os.getpid(), persistent=True,
                    response_completion='validated_final_reply')

    def envelope(self, body):
        return envelope(body, self.epoch)

    async def receive(self, request):
        if (not isinstance(request, dict) or request.get('protocol') != PROTOCOL
                or request.get('ledger_epoch') not in (self.epoch, None)
                or not isinstance(request.get('call_id'), str)
                or len(request['call_id']) != 32
                or not isinstance(request.get('body'), dict)):
            response = dict(ok=False, error='invalid admission protocol or ledger epoch')
        elif request.get('ledger_epoch') is None and request['body'].get('op') != 'ping':
            response = dict(ok=False, error='ledger epoch is required for mutations')
        else:
            # No await/thread handoff while the existing ledger checks/debits.
            response = self.handler(request['body'])
        # self.handler has completed the ledger mutation and score update.
        # This frame is the operation's final application-level result.
        yield dict(response, rpc_final=True, rpc_protocol=PROTOCOL,
                   rpc_call_id=request.get('call_id') if isinstance(request, dict) else None,
                   rpc_transport='dynamo', rpc_request_plane='tcp', rpc_codec='msgpack',
                   rpc_instance_id=self.instance_id, rpc_ledger_epoch=self.epoch,
                   rpc_server_pid=os.getpid())

    async def close(self):
        if self.runtime is not None:
            self.runtime.shutdown()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class DynamoAdmissionRPC:
    """A blocking caller waits on a dedicated, persistent async Runtime thread."""
    def __init__(self, name, timeout_s, *, instance_id=None, ledger_epoch=None):
        self.name, self.timeout_s = name, timeout_s
        self.instance_id, self.epoch = instance_id, ledger_epoch
        self.pid = os.getpid()
        self.closed = False
        self.runtime = None
        self.streams = None
        self.phase = dict(stage='startup')
        self.loop = asyncio.new_event_loop()
        self.ready = Future()
        self.thread = threading.Thread(target=self.run, name='admission-runtime', daemon=True)
        self.thread.start()
        try:
            self.ready.result(timeout=timeout_s)
            from sim.stack.admission_rpc_status import register
            register(self)
        except BaseException:
            self.close(suppress_errors=True)
            raise

    def run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.streams = FinalReplyStreams(self.timeout_s, observe=self.observe)
            self.loop.run_until_complete(self.connect())
            self.ready.set_result(True)
            self.loop.run_forever()
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            if self.runtime is not None:
                self.runtime.shutdown()
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()

    async def connect(self):
        async with asyncio.timeout(self.timeout_s):
            self.runtime = new_runtime(self.loop, client=True)
            self.client = await self.runtime.endpoint(self.name).client()
            await self.client.wait_for_instances()
            ids = self.client.instance_ids()
            if len(ids) != 1 or (self.instance_id is not None and ids[0] != self.instance_id):
                raise RuntimeError('admission discovery does not match the single pinned ledger')
            self.instance_id = ids[0]
            response = await self.invoke({'op': 'ping'}, handshake=True)
            epoch = response.get('rpc_ledger_epoch')
            if not isinstance(epoch, str) or not epoch or (self.epoch is not None and epoch != self.epoch):
                raise RuntimeError('admission ledger startup identity mismatch')
            self.epoch = epoch

    async def invoke(self, body, *, handshake=False):
        self.check()
        if set(self.client.instance_ids()) != {self.instance_id}:
            raise ConnectionError('admission ledger membership changed')
        self.phase = dict(stage='membership_checked',kind=body.get('kind'),op=body.get('op'),
                          event_id=body.get('event_id'),request_id=body.get('request_id'))
        request = envelope(body, self.epoch)
        validate = lambda response: validate_reply(response, request, self.instance_id, handshake=handshake)
        if handshake:
            response = await one_reply(self.client, self.instance_id, request, self.phase)
            validate(response)
        else:
            response = await self.streams.request(self.client, self.instance_id, request, validate, self.phase)
        return dict(response, rpc_client_pid=self.pid, rpc_bridge='thread')

    def observe(self, phase, **fields):
        if os.environ.get('SIM_FINE_TRACE_DIR'):
            from sim.stack import fine_trace
            fine_trace.emit('admission_rpc', phase=phase, **fields)

    def check(self):
        if self.closed or os.getpid() != self.pid:
            raise ConnectionError('closed or inherited admission Runtime client')
        if self.streams is not None:
            self.streams.check()

    def settle(self):
        self.check()
        future = asyncio.run_coroutine_threadsafe(self.streams.settle(), self.loop)
        return future.result(timeout=self.timeout_s + 1)

    def exchange(self, body):
        self.check()
        future = asyncio.run_coroutine_threadsafe(self.invoke(body), self.loop)
        try:
            return future.result(timeout=self.timeout_s)
        except BaseException as exc:
            progress = dict(self.phase)
            future.cancel()
            self.close(suppress_errors=True)
            if isinstance(exc, TimeoutError):
                raise TimeoutError(f'native admission RPC timed out: {progress}') from exc
            raise

    def close(self, *, suppress_errors=False):
        if self.closed:
            return
        self.closed = True
        error = None
        if self.loop.is_running():
            if threading.current_thread() is not self.thread and self.streams is not None:
                future = asyncio.run_coroutine_threadsafe(self.streams.settle(), self.loop)
                try:
                    future.result(timeout=self.timeout_s + 1)
                except BaseException as exc:
                    error = exc
                    future.cancel()
            self.loop.call_soon_threadsafe(self.loop.stop)
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=self.timeout_s)
        if error is not None and not suppress_errors:
            raise RuntimeError('admission stream cleanup failed at close') from error
