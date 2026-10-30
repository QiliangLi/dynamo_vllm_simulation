"""Two-worker functional checks after removing execution-feedback RPCs.

Not a Mooncake benchmark or a measurement of policy benefits.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from run_tfs_stack import run_one, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--transport', choices=('dynamo', 'tcp', 'unix'), default='dynamo')
    parser.add_argument('--cases', nargs='+', choices=('bulk-immediate','pipeline-immediate','pipeline-tfs','pipeline-force'),
                        help='run selected functional cases; omitted means all four')
    args = parser.parse_args()
    if os.environ.get('PYTHONHASHSEED') != '0':
        parser.error('set PYTHONHASHSEED=0 to match worker block hashes')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT/'configs/mooncake_first_seen_layerwise.json').read_text())
    config.update(workers=2, router_mode='round-robin', max_model_len=2048,
                  num_blocks=8193, block_size=16, max_num_batched_tokens=256,
                  wall_time_scale=1, policy='priority', request_ordering='edf')
    config['request_profile']['profile_token_budget'] = 256
    config['admission'].update(aging_enabled=False, feedback_delay_s=0,
                               rpc_transport=args.transport, reference_bandwidth_Bps=1.)
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(ROOT/'configs/model'))
    rng = random.Random(20261029)
    results = []
    cases = [('bulk-immediate', 'bulk', 'immediate', False),
             ('pipeline-immediate', 'layer_pipeline', 'immediate', False),
             ('pipeline-tfs', 'layer_pipeline', 'tfs', False),
             ('pipeline-force', 'layer_pipeline', 'tfs', True)]
    for name, kv, mode, forced in cases:
        if args.cases and name not in args.cases:
            continue
        c = deepcopy(config)
        c['kv_transfer_mode'] = kv
        if mode == 'tfs':
            c['admission'].update(placement='scheduler', classification='arrival', idle_bypass=forced)
        shapes = [(512, 16)]*6 if forced else [(512, 16), (128, 128), (0, 64), (64, 384)]*2
        rows = []
        for i, (h, u) in enumerate(shapes):
            tokens = [rng.choice([1332, 995, 2581, 640, 1366, 23748]) for _ in range(h+u)]
            assert tokenizer.encode(tokenizer.decode(tokens)) == tokens
            rows.append(dict(id=f'{name}-{i}', arrival_s=0., prompt_token_ids=tokens,
                output_tokens=1, remote_prefix_tokens=h, hit_tokens=h, u_tokens=u,
                full_hit_adjusted=False, cache_model='first_seen', class_id=str(i%4)))
        trace = out/(name+'.jsonl')
        trace.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        folder = out/name
        result = run_one(c, trace, folder, Path(sys.executable), mode, 'first_token', 2)
        raw = json.loads((folder/'admission.runtime.final.json').read_text())
        observed = json.loads((folder/'admission.final.json').read_text())
        assert raw['execution_feedback'] == 'disabled'
        assert all(r['load_attempts']==0 and r['submitted_read_bytes']==0
                   and r['load_submit_s'] is None and r['load_done_s'] is None
                   for r in raw['records'].values())
        assert not any(e['event']=='feedback_received' and e.get('engine_id') for e in raw['events'])
        assert observed['load_observation_source'] == 'offline_worker_logs'
        assert result['workers'] == [0, 1]
        audit = json.loads((folder/'read_admission.check.json').read_text()) if mode=='tfs' else None
        if forced:
            assert audit['forced_grants'] > 0 and audit['leaked_permits'] == 0
        elif mode == 'immediate':
            assert not raw['permits']
        drains=json.loads((folder/'admission_rpc.check.json').read_text())
        results.append(dict(case=name, status='pass', requests=len(rows), block_size=16,
            wall_time_scale=1, rpc_transport=args.transport, execution_feedback_rpcs=0,
            observed_bytes=result['actual_submitted_read_bytes'], audit=audit, response_drains=drains))
        write(out/'report.json', dict(status='running', cases=results))
        print(json.dumps(results[-1]), flush=True)
    write(out/'report.json', dict(status='pass', scope='functional_only', cases=results))


if __name__ == '__main__':
    main()
