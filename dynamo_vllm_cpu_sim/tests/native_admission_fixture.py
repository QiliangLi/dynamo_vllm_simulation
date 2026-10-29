"""Isolated real Runtime processes used by test_admission_dynamo.py."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]


def serve(connection, endpoint, mode):
    async def main():
        from sim.stack.admission_service import create_app
        import test_stack_admission as base
        if mode == 'timeout':
            from sim.stack.dynamo_admission_rpc import DynamoAdmissionServer
            original = DynamoAdmissionServer.receive
            async def delayed(self, request):
                async for response in original(self, request):
                    if request['body'].get('op') == 'acquire':
                        await asyncio.sleep(1.2)  # Mutate first, lose/delay the reply.
                    yield response
            DynamoAdmissionServer.receive = delayed
        config = base.config()
        config['scheduler'] = 'upstream'
        config['admission'].update(placement='scheduler', classification='arrival')
        app = create_app(config, 'http://127.0.0.1:1', feedback_endpoint=endpoint)
        async with app.router.lifespan_context(app):
            state = app.state.admission
            for rid in ('a', 'b'):
                state.arrive(dict(id=rid, prompt_token_ids=list(range(100)), remote_prefix_tokens=32))
            state.admit()
            connection.send(dict(rpc=state.rpc_info, identities=state.uuid_by_id,
                                 hints=state.controller.hints))
            while True:
                if connection.poll():
                    command = connection.recv()
                    if command[0] == 'stop':
                        break
                    if command[0] == 'first_token':
                        state.local_event('first_token', command[1])
                    connection.send(state.snapshot())
                await asyncio.sleep(.002)
    asyncio.run(main())


def main(mode):
    from sim.stack.feedback import FeedbackClient
    with tempfile.TemporaryDirectory(prefix='native-admission-') as folder:
        os.environ.update(DYN_DISCOVERY_BACKEND='file', DYN_FILE_KV=folder,
                          DYN_REQUEST_PLANE_CODEC='msgpack', DYN_SYSTEM_PORT='-1')
        # Hosts for native Runtime are resolved by the pinned library itself.
        for name in ('DYN_TCP_RPC_HOST', 'DYN_TCP_RESPONSE_STREAM_HOST'):
            os.environ.pop(name, None)
        endpoint = f'dyn://test-{uuid.uuid4().hex}.ledger.control'
        connection, child = multiprocessing.get_context('spawn').Pipe()
        process = multiprocessing.get_context('spawn').Process(target=serve, args=(child, endpoint, mode))
        process.start()
        child.close()
        clients = []
        try:
            assert connection.poll(20), 'native admission server did not become ready'
            info = connection.recv()
            rpc = info['rpc']
            def make(worker=0, **kwargs):
                client = FeedbackClient(endpoint, worker, **kwargs)
                clients.append(client)
                return client
            def acquire(client, rid):
                return client.control('acquire', info['identities'][rid], acquisition_key='first',
                                      load_attempt_id=1, storage_hint=info['hints'][rid])
            def snapshot(*command):
                connection.send(command or ('snapshot',))
                assert connection.poll(5)
                return connection.recv()
            if mode == 'identity':
                for bad in (dict(instance_id=rpc['instance_id']+1), dict(ledger_epoch='wrong-epoch')):
                    try:
                        make(**bad)
                    except (RuntimeError, ConnectionError):
                        pass
                    else:
                        raise AssertionError('wrong ledger identity was accepted')
                assert not snapshot()['permits']
            elif mode == 'timeout':
                client = make(timeout_s=.4, instance_id=rpc['instance_id'], ledger_epoch=rpc['ledger_epoch'])
                try:
                    acquire(client, 'a')
                except TimeoutError:
                    pass
                else:
                    raise AssertionError('delayed response did not time out')
                assert client.closed and not client.native.thread.is_alive()
                try:
                    acquire(client, 'a')
                except ConnectionError:
                    pass
                else:
                    raise AssertionError('closed client replayed an ambiguous request')
                state = snapshot()
                assert len(state['permits']) == state['snapshot']['heavy_held'] == 1
            else:
                a = make(0, instance_id=rpc['instance_id'], ledger_epoch=rpc['ledger_epoch'])
                b = make(1, instance_id=rpc['instance_id'], ledger_epoch=rpc['ledger_epoch'])
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(acquire, a, 'a'), pool.submit(acquire, b, 'b')]
                    replies = [f.result() for f in futures]
                assert sum(r['granted'] for r in replies) == 1
                winner = next(i for i,r in enumerate(replies) if r['granted'])
                loser = 1-winner
                ids, calls = ('a','b'), (a,b)
                rid, client = ids[winner], calls[winner]
                token = replies[winner]['permit_id']
                replay = acquire(client, rid)
                assert replay['permit_id'] == token and replay['replay']
                client.submitted(info['identities'][rid], 1024)
                client.completed(info['identities'][rid])
                assert not acquire(calls[loser], ids[loser])['granted']
                state = snapshot('first_token', rid)
                feedback = [e for e in state['events'] if e['event']=='feedback_received' and e.get('engine_id')]
                assert len(feedback)==2 and all(e['rpc_transport']=='dynamo' for e in feedback)
                next_reply = acquire(calls[loser], ids[loser])
                assert next_reply['granted']
                for _ in range(2):
                    calls[loser].control('rollback', info['identities'][ids[loser]], permit_id=next_reply['permit_id'])
                assert snapshot()['snapshot']['heavy_held'] == 0
                for reply in replies:
                    assert reply['rpc_server_pid']==process.pid!=reply['rpc_client_pid']==os.getpid()
                    assert reply['rpc_bridge']=='thread' and reply['rpc_codec']=='msgpack'
            for client in clients:
                client.close()
                assert not client.native.thread.is_alive()
            print(json.dumps(dict(status='pass', mode=mode, server_pid=process.pid, client_pid=os.getpid())), flush=True)
        finally:
            for client in clients:
                client.close()
            if process.is_alive():
                connection.send(('stop',))
                process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
            connection.close()


if __name__ == '__main__':
    main(sys.argv[1])
