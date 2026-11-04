"""Validation only: publish seeded storage blocks as real Dynamo STORAGE events.

This is not a real storage backend or an automatic production event adapter.
The normal worker entry point never imports this file.
"""
import importlib
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import vllm.platforms
from vllm.platforms.cpu import CpuPlatform
vllm.platforms._current_platform = CpuPlatform()

main_module = importlib.import_module('dynamo.vllm.main')


def setup_fixture(config, generate_endpoint, vllm_config, **kwargs):
    import msgspec
    import zmq
    from dynamo.llm import KvEventPublisher
    from vllm.distributed.kv_events import BlockStored, EventBatch
    from sim.trace_io import load_trace
    trace = Path(os.environ['SIM_TIER_FIXTURE_TRACE'])
    rows = load_trace(trace)
    cfg = json.loads(Path(os.environ['SIM_STACK_CONFIG']).read_text())
    b = cfg['block_size']
    events = []
    for row in rows:
        count = row.get('remote_prefix_tokens', 0)//b
        if not count:
            continue
        chain = hashlib.sha256()
        hashes = []
        for i in range(count):
            for token in row['prompt_token_ids'][i*b:(i+1)*b]:
                chain.update(token.to_bytes(4, 'big'))
            hashes.append(int.from_bytes(chain.digest()[:8], 'big', signed=True))
        events.append(BlockStored(
            block_hashes=hashes,
            parent_block_hash=None, token_ids=row['prompt_token_ids'][:count*b],
            block_size=b, lora_id=None, medium='STORAGE', lora_name=None))
    context = zmq.Context.instance()
    # Socket ownership stays with its publisher thread.
    ready = threading.Event()
    endpoint = []
    def publish():
        socket = context.socket(zmq.PUB)
        port = socket.bind_to_random_port('tcp://127.0.0.1')
        endpoint.append(f'tcp://127.0.0.1:{port}')
        ready.set()
        sequence = 0
        while True:
            time.sleep(.25)
            data = msgspec.msgpack.encode(EventBatch(ts=time.time(),events=events,data_parallel_rank=0))
            socket.send_multipart([b'', sequence.to_bytes(8,'big'), data])
            sequence += 1
    threading.Thread(target=publish, daemon=True).start()
    if not ready.wait(5):
        raise RuntimeError('fixture event publisher did not bind')
    config.use_kv_events = True
    config.enable_local_indexer = True
    publisher = KvEventPublisher(endpoint=generate_endpoint, kv_block_size=b,
        zmq_endpoint=endpoint[0], zmq_topic='', enable_local_indexer=True, dp_rank=0)
    return [publisher]


main_module.setup_kv_event_publisher = setup_fixture
runpy.run_path(str(ROOT/'scripts/stack_vllm_worker.py'), run_name='__main__')
