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
        received_ops=[]
        tail_release=asyncio.Event()
        tails_started=[];tails_ended=[]
        if mode.startswith('tail'):
            from sim.stack.dynamo_admission_rpc import DynamoAdmissionServer
            original = DynamoAdmissionServer.receive
            async def trailing(self, request):
                op=request['body'].get('op');received_ops.append(op)
                async for response in original(self, request):
                    yield response
                    if op in ('acquire','rollback'):
                        tails_started.append(request['call_id'])
                        await tail_release.wait()
                        if mode=='tail-extra':yield response
                        tails_ended.append(request['call_id'])
            DynamoAdmissionServer.receive=trailing
        if mode == 'force':
            from sim.stack.dynamo_admission_rpc import DynamoAdmissionServer
            original = DynamoAdmissionServer.receive
            async def recorded(self, request):
                received_ops.append(request['body'].get('op'))
                async for response in original(self, request):
                    if request['body'].get('op') == 'force_acquire':
                        await asyncio.sleep(.05)
                    yield response
            DynamoAdmissionServer.receive = recorded
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
        if mode == 'force':
            config['kv_transfer_mode']='layer_pipeline'
            config['enable_prefix_caching']=False
            config['storage']['bandwidth_policy']='max_min_fair'
            config['admission']['idle_bypass']=True
        app = create_app(config, 'http://127.0.0.1:1', feedback_endpoint=endpoint)
        async with app.router.lifespan_context(app):
            state = app.state.admission
            for rid in (('a', 'b', 'c') if mode == 'force' else ('a', 'b')):
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
                    if command[0] == 'release_tails':tail_release.set()
                    connection.send(dict(state.snapshot(),control_ops=list(received_ops),
                                         tails_started=list(tails_started),tails_ended=list(tails_ended)))
                await asyncio.sleep(.002)
    asyncio.run(main())


def main(mode):
    from sim.stack.feedback import FeedbackClient
    with tempfile.TemporaryDirectory(prefix='native-admission-') as folder:
        os.environ.update(DYN_DISCOVERY_BACKEND='file', DYN_FILE_KV=folder,
                          DYN_REQUEST_PLANE_CODEC='msgpack', DYN_SYSTEM_PORT='-1')
        if mode.startswith('tail'):
            os.environ['SIM_STACK_ADMISSION_STATUS_DIR']=str(Path(folder)/'drain-status')
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
            if mode.startswith('tail'):
                client=make(0,timeout_s=2,instance_id=rpc['instance_id'],ledger_epoch=rpc['ledger_epoch'])
                client.enable_idle_bypass()
                if mode=='tail-timeout':client.native.streams.timeout_s=.1
                reply=acquire(client,'a')
                assert reply['granted'] and reply['rpc_final']
                if mode=='tail':
                    assert not acquire(client,'b')['granted']
                    client.control('rollback',info['identities']['a'],permit_id=reply['permit_id'])
                    assert acquire(client,'b')['granted']
                    state=snapshot()
                    assert not state['tails_ended']
                    assert [op for op in state['control_ops'] if op!='ping']==['acquire','acquire','rollback','acquire']
                    assert client.native.streams.snapshot()['pending_streams']==4
                    snapshot('first_token','b')
                    snapshot('release_tails')
                    stats=client.native.settle()
                    assert stats['final_replies']==stats['drained_streams']==4
                    assert stats['pending_streams']==0 and not stats['failure']
                    from sim.stack.admission_rpc_status import collect
                    assert collect(Path(folder)/'drain-status',2)['status']=='passed'
                else:
                    if mode=='tail-extra':snapshot('release_tails')
                    try:client.native.settle()
                    except RuntimeError:pass
                    else:raise AssertionError('bad or missing stream end was ignored')
                    try:acquire(client,'a')
                    except RuntimeError:pass
                    else:raise AssertionError('late failure allowed another mutation')
                    state=snapshot()
                    assert len(state['permits'])==state['snapshot']['heavy_held']==1
                    assert state['control_ops'].count('acquire')==1  # No replay or rollback.
                    from sim.stack.admission_rpc_status import collect
                    try:collect(Path(folder)/'drain-status',2)
                    except RuntimeError:pass
                    else:raise AssertionError('final audit accepted a failed background drain')
            elif mode == 'identity':
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
            elif mode == 'force':
                client = make(0,instance_id=rpc['instance_id'],ledger_epoch=rpc['ledger_epoch'])
                client.enable_idle_bypass()
                proof=dict(running_before=0,scheduled_tokens_before=0,nonheavy_candidates=0,
                           heavy_candidates=2,allocated=True,external_tokens=32)
                tokens=[]
                for rid in ('a','b'):
                    token,_=client.force_admit_async(info['identities'][rid],info['hints'][rid],proof)
                    tokens.append(token)
                assert len(set(tokens)) == 2
                assert not acquire(client,'c')['granted']  # Ordered barrier after both notices.
                state=snapshot()
                assert state['snapshot']['heavy_held'] == len(state['permits']) == 2
                assert {p['worker'] for p in state['permits'].values()} == {0}
                state=snapshot('first_token','b')
                assert state['snapshot']['heavy_held'] == 1
                assert not acquire(client,'c')['granted']
                state=snapshot('first_token','a')
                assert state['snapshot']['heavy_held'] == 0
                assert acquire(client,'c')['granted']
                state=snapshot('first_token','c')
                assert state['snapshot']['heavy_held'] == 0
                assert all(p['state']=='released' for p in state['permits'].values())
                assert 'force_status' not in state['control_ops']
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
                assert not acquire(calls[loser], ids[loser])['granted']
                state = snapshot('first_token', rid)
                feedback = [e for e in state['events'] if e['event']=='feedback_received' and e.get('engine_id')]
                assert feedback == []
                next_reply = acquire(calls[loser], ids[loser])
                assert next_reply['granted']
                for _ in range(2):
                    calls[loser].control('rollback', info['identities'][ids[loser]], permit_id=next_reply['permit_id'])
                assert snapshot()['snapshot']['heavy_held'] == 0
                for reply in replies:
                    assert reply['rpc_server_pid']==process.pid!=reply['rpc_client_pid']==os.getpid()
                    assert reply['rpc_bridge']=='thread' and reply['rpc_codec']=='msgpack'
            for client in clients:
                try:client.close()
                except RuntimeError:
                    if mode not in ('tail-extra','tail-timeout'):raise
                assert not client.native.thread.is_alive()
            print(json.dumps(dict(status='pass', mode=mode, server_pid=process.pid, client_pid=os.getpid())), flush=True)
        finally:
            for client in clients:
                try:client.close()
                except RuntimeError:pass
            if process.is_alive():
                connection.send(('stop',))
                process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
            connection.close()


if __name__ == '__main__':
    main(sys.argv[1])
