"""Build workloads and send HTTP requests. Does not manage or inspect services."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import engine as e

ROOT = Path(__file__).resolve().parent
CASES = ('pilot24', 'mooncake_abc', 'mooncake_abcd', 'synthetic_abc', 'agentx', 'synthetic_abc_3w')
TOKENIZER = '/home/LLM_Weight/MiniMax-M2.5-w8a8-QuaRot-l16'


def build(a, out, tok=None, pool=None):
    catalog = e.read_json(ROOT / 'catalogs' / (a.benchmark + '.json'))
    logical = catalog['requests']
    previous = -1
    for r in logical:
        h, u, at = r['hit_tokens'], r['miss_tokens'], r['arrival_offset_ms']
        if type(h) is not int or type(u) is not int or h < 0 or u < 1 or h % 128 or r['block_size'] != 128:
            raise ValueError('Invalid H/U/block size')
        if not math.isfinite(at) or at < 0 or at < previous or r['max_tokens'] != 1:
            raise ValueError('Invalid schedule/output length')
        if max(h + u, h + 1 if h else 0) + 1 > catalog['max_model_len']:
            raise ValueError('Context limit exceeded')
        previous = at
    tok = tok or e.tokenizer_at(a.tokenizer)
    pool = pool or e.safe_pool(tok)
    identity = e.tokenizer_identity(a.tokenizer, tok)
    nonce = uuid.uuid4().hex
    shape_fields = ('request_class', 'hit_tokens', 'miss_tokens', 'block_size',
                    'add_special_tokens', 'max_tokens', 'arrival_offset_ms')
    workload_id = 'workload_' + e.digest([{k: r[k] for k in shape_fields} for r in logical])
    seen, rows = set(), []
    for i, r in enumerate(logical):
        fresh = e.build_request(tok, pool, r['hit_tokens'], r['miss_tokens'], 128,
                                random.Random(f'{a.seed}/{nonce}/{i}'), r['add_special_tokens'], seen)
        profile = dict(tokenizer=identity, add_special_tokens=r['add_special_tokens'], block_size=128)
        fresh.update(profile_id=e.digest(profile), block_size=128,
                     add_special_tokens=r['add_special_tokens'], max_tokens=1)
        fresh['request_id'] = 'req_' + e.digest(fresh)[:32]
        fresh.update(logical_id=f'q{i:05d}', request_class=r['request_class'],
                     arrival_offset_ms=r['arrival_offset_ms'], slo_ms=None)
        rows.append(fresh)
        if (i + 1) % 8 == 0 or i + 1 == len(logical):
            print(f'GENERATE {i+1}/{len(logical)}', flush=True)
    trace = out / 'trace'
    e.write_trace(trace, dict(benchmark=a.benchmark, workload_id=workload_id, seed=a.seed,
                             nonce=nonce, tokenizer=identity, single_use_replay=True,
                             max_model_len=catalog['max_model_len']), rows)
    e.validate(SimpleNamespace(trace=trace, tokenizer=None))
    return trace


def phase(out, name, trace, config, label, prepared=None):
    args = [sys.executable, '-u', str(Path(__file__).resolve()), '_phase', '--trace', str(trace),
            '--phase', name, '--config', str(config), '--output', str(out / name), '--label', label]
    if prepared:
        args += ['--prepared-run', str(prepared)]
    print('PHASE=' + name, flush=True)
    with (out / (name + '.log')).open('w', encoding='utf-8') as log:
        process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        began = time.monotonic()
        while True:
            try:
                code = process.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                print(f'RUNNING {name} elapsed_s={time.monotonic()-began:.0f}', flush=True)
        if code:
            print('\n'.join((out / (name + '.log')).read_text(encoding='utf-8').splitlines()[-15:]))
            raise RuntimeError(f'{name} failed; inspect {out / (name + ".log")}')


def send(a, out, trace):
    _, rows = e.load_trace(trace)
    if (trace / '.replay_claim.json').exists():
        raise ValueError('Trace already replayed; build fresh prefixes')
    cfg = dict(endpoint=a.endpoint, model=a.model, timeout_s=a.timeout, time_scale=a.time_scale,
               order='original', save_sse=True)
    prepare_extra, replay_extra = {}, {}
    if a.replay_phase2_dump is not None:
        prepare_extra = {'kv_transfer_params': {'enable_phase2_dump': True}}
        replay_extra = {'kv_transfer_params': {'enable_phase2_dump': a.replay_phase2_dump == 'on'}}
    prepared = None
    if any(r['hit_tokens'] for r in rows):
        e.write_json(out / 'prepare_config.json', dict(cfg, mode='concurrent',
                     concurrency=a.prepare_concurrency, cache_condition='unspecified',
                     body_extra=prepare_extra))
        phase(out, 'prepare', trace, out / 'prepare_config.json', a.label)
        prepared = out / 'prepare'
        print(f'PREPARE_COMPLETE; settle {a.settle_s}s (not proof of actual cache persistence)', flush=True)
        time.sleep(a.settle_s)
    e.write_json(out / 'replay_config.json', dict(cfg, mode=a.mode, concurrency=a.concurrency,
                 cache_condition='storage' if prepared else 'unspecified', body_extra=replay_extra))
    phase(out, 'replay', trace, out / 'replay_config.json', a.label, prepared)
    print((out / 'replay/summary.md').read_text(encoding='utf-8'), flush=True)
    print('SUMMARY=' + str(out / 'replay/summary.md'), flush=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True, metavar='{build,run,send,summary}')
    for name in ('build', 'run', 'send'):
        q = sub.add_parser(name)
        q.add_argument('--output', type=Path)
        q.add_argument('--label', default='', help='Saved note only; does not change traffic or service settings')
        if name != 'send':
            q.add_argument('--benchmark', choices=CASES, default='pilot24')
            q.add_argument('--tokenizer', default=TOKENIZER)
            q.add_argument('--seed', type=int, default=20261112)
        else:
            q.add_argument('--trace', required=True, type=Path)
        if name != 'build':
            q.add_argument('--endpoint', default='http://127.0.0.1:8000/v1/completions')
            q.add_argument('--model', default='minimax')
            q.add_argument('--prepare-concurrency', type=int, default=1)
            q.add_argument('--concurrency', type=int, default=360)
            q.add_argument('--mode', choices=('paced', 'serial', 'burst', 'concurrent'), default='paced')
            q.add_argument('--time-scale', type=float, default=1)
            q.add_argument('--settle-s', type=float, default=10)
            q.add_argument('--replay-phase2-dump', choices=('on', 'off'),
                           help='Set replay KV saving; prepare explicitly enables saving. '
                                'Omit to preserve server defaults in both phases.')
            q.add_argument('--timeout', type=float, default=1800)
    q = sub.add_parser('_phase')
    q.add_argument('--trace', required=True)
    q.add_argument('--phase', choices=('prepare', 'replay'), required=True)
    q.add_argument('--config', required=True)
    q.add_argument('--output', required=True)
    q.add_argument('--label', default='')
    q.add_argument('--prepared-run')
    q = sub.add_parser('summary')
    q.add_argument('runs', nargs='+', type=Path)
    return p


def main():
    a = parser().parse_args()
    if a.command == '_phase':
        import asyncio
        a.classes = None
        a.cache_ready_note = ('Prefix HTTP requests completed; propagation wait only. '
                              'Actual OD persistence/hits and device cache state not verified.')
        asyncio.run(e.run_async(a))
        return
    if a.command == 'summary':
        for out in a.runs:
            print('\n=== ' + str(out) + ' ===\n')
            print((out / 'replay/summary.md').read_text(encoding='utf-8'))
        return
    if a.command != 'build':
        if a.concurrency < 1 or a.prepare_concurrency < 1 or not math.isfinite(a.timeout) or a.timeout <= 0:
            raise ValueError('Concurrency and timeout must be positive')
        if not math.isfinite(a.settle_s) or a.settle_s < 0 or not math.isfinite(a.time_scale) or a.time_scale <= 0:
            raise ValueError('Invalid settle/time-scale')
    out = (a.output or ROOT / 'results' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])).resolve()
    if a.command == 'send' and (a.trace / '.replay_claim.json').exists():
        raise ValueError('Trace already replayed; build fresh prefixes')
    out.mkdir(parents=True, exist_ok=False)
    print('OUTPUT=' + str(out), flush=True)
    e.write_json(out / 'arguments.json', {k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items()})
    trace = a.trace.resolve() if a.command == 'send' else build(a, out)
    if a.command != 'build':
        send(a, out, trace)


if __name__ == '__main__':
    main()
