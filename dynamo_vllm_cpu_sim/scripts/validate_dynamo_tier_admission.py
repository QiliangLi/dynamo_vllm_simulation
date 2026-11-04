"""Two-worker real-router smoke; cache events and KV IO are explicit fixtures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import re
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from run_tfs_stack import run_one, write


def check_pre_score(folder, requests, factor=.1):
    from check_stack_routing import ANSI
    resolved,scored,picked,booked={},{},{},{}
    for index,line in enumerate(ANSI.sub('',(folder/'frontend.log').read_text()).splitlines()):
        match=re.search(r'request_id=(?:"([^\"]+)"|(\S+))',line)
        if not match:continue
        rid=match.group(1) or match.group(2)
        if 'TIER_PROFILE_RESOLVED' in line:
            assert rid not in resolved,'multiple profile resolutions for one request'
            resolved[rid]=index
        if 'TIER_REQUEST_SCORE worker_id=' in line:
            values={k:float(v) for k,v in re.findall(r'(own_prefill_credit|own_decode_credit|baseline|weighted)=([0-9.eE+-]+)',line)}
            heavy='heavy=true' in line
            assert values['weighted']<=values['baseline']+1e-6
            if not heavy: assert values['own_prefill_credit']==values['own_decode_credit']==0
            worker=re.search(r'worker_id=(\d+)',line).group(1)
            scored.setdefault(rid,[]).append(dict(index=index,heavy=heavy,worker_id=worker,**values))
        if '[ROUTING] Best:' in line:picked[rid]=index
        if 'TIER_PROFILE_BOOKED' in line:booked[rid]=index
    assert len(resolved)==len(scored)==len(picked)==len(booked)==requests
    for rid,rows in scored.items():
        assert resolved[rid]<min(r['index'] for r in rows)
        # The host's "Best" report is emitted after the native actor returns;
        # native selection precedes booking inside that actor.
        assert max(r['index'] for r in rows)<booked[rid]
        assert max(r['index'] for r in rows)<picked[rid]
    own_discounted=sum(r['weighted']<r['baseline']-1e-6 for rows in scored.values() for r in rows)
    if factor < 1:
        assert own_discounted>0,'no current-request discount before picker'
    else:
        assert own_discounted==0,'alpha=1 must preserve the current-request cost'
    for rows in scored.values():
        assert len({r['heavy'] for r in rows})==1,'workers must use one shared heavy mark'
    state=json.loads((folder/'admission.final.json').read_text())
    for event in ['dynamo_tier_profile_resolved','storage_hint_created']:
        events=[e for e in state['events'] if e['event']==event]
        assert len(events)==len({e['id'] for e in events})==requests
    assert not any(e['event']=='tier_profiles_prepared' for e in state['events'])
    return dict(status='passed',requests=requests,factor=factor,
        single_profile_per_request=True,same_mark_for_all_workers=True,
        current_request_discounted_rows=own_discounted,
        order='shared lookup -> one classification/registration -> scorer -> picker -> full booking')



def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--routing-python-path', required=True, type=Path)
    p.add_argument('--event-hit-fraction',type=float,default=1.,
                   help='fixture index coverage relative to storage; <1 tests tolerated lookup bias')
    p.add_argument('--factor',type=float,default=.1,help='waiting/current-heavy score factor')
    args = p.parse_args()
    if not 0 <= args.event_hit_fraction <= 1:
        p.error('--event-hit-fraction must be in [0,1]')
    if not 0 <= args.factor <= 1:
        p.error('--factor must be in [0,1]')
    out = args.output.resolve(); out.mkdir(parents=True,exist_ok=False)
    cfg = json.loads((ROOT/'configs/sa_abc_stack.json').read_text())
    cfg.pop('cache_model',None)
    cfg.update(workers=2,router_mode='kv',request_ordering='edf',policy='priority',
               max_model_len=20000,num_blocks=100000,wall_time_scale=20)
    cfg['admission'].update(placement='scheduler',classification='arrival',
                            profile_source='dynamo_tier',aging_enabled=False)
    cfg['routing_accounting'] = dict(enabled=True,waiting_factor=args.factor,
        python_path=str(args.routing_python_path.resolve()))
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(str(ROOT/'configs/model'))
    rows = []
    for i in range(8):
        heavy = i < 6
        n, h = (1040,1024) if heavy else (512,0)
        prefix = tok.encode(f' request {i} unique prefix' if heavy else f' zebra {i} light input')
        motif = tok.encode(' hello world test data time request')
        tokens = (prefix + motif*n)[:n]
        assert tok.encode(tok.decode(tokens)) == tokens
        # A later light probe must route while classified heavy requests wait.
        # Otherwise a one-wave burst can finish every picker call before any
        # async profile callback returns, providing no discounted-score evidence.
        rows.append(dict(id=f'tier-smoke-{i}',arrival_s=.1 if heavy else .11,
            prompt_token_ids=tokens,remote_prefix_tokens=h,output_tokens=1))
    from sim.request_profile import normalize_config,build_profile
    profile = build_profile(rows[0],normalize_config(cfg))
    cfg['admission']['reference_bandwidth_Bps'] = profile.pressure_Bps
    trace = out/'trace.jsonl'; trace.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    original = subprocess.Popen
    def start(command, **kwargs):
        command = list(command)
        if 'scripts/stack_vllm_worker.py' in command:
            command[command.index('scripts/stack_vllm_worker.py')] = 'scripts/tier_fixture_worker.py'
            kwargs['env'] = dict(kwargs['env'], SIM_TIER_FIXTURE_TRACE=str(trace),
                                SIM_TIER_FIXTURE_HIT_FRACTION=str(args.event_hit_fraction))
        return original(command, **kwargs)
    with patch('run_tfs_stack.subprocess.Popen',start):
        result = run_one(cfg,trace,out/'run',Path(sys.executable),'tfs','first_token',2)
    state = json.loads((out/'run/admission.final.json').read_text())
    profiles = [e for e in state['events'] if e['event']=='dynamo_tier_profile_resolved']
    assert len(profiles)==8
    b=cfg['block_size']
    expected=[int((r['remote_prefix_tokens']//b)*args.event_hit_fraction)*b for r in rows]
    assert [state['records'][r['id']]['profile']['estimated_remote_tokens'] for r in rows] == expected
    assert sum(r['submitted_read_bytes'] for r in state['records'].values()) == sum(
        r['remote_prefix_tokens']*cfg['storage']['kv_bytes_per_token'] for r in rows)
    mismatches=[json.loads(line) for path in (out/'run').glob('worker*.read_admission.jsonl')
                for line in path.read_text().splitlines()
                if json.loads(line)['event']=='tier_lookup_mismatch']
    assert len({e['request_id'] for e in mismatches}) == sum(
        est!=r['remote_prefix_tokens'] for est,r in zip(expected,rows))
    from check_routing_accounting import check
    score = check(out/'run',require_discount=args.event_hit_fraction==1. and args.factor<1)
    pre_score=check_pre_score(out/'run',8,args.factor)
    write(out/'run/tier_prescore.check.json',pre_score)
    write(out/'validation.json',dict(status='passed',requests=8,workers=2,
        profile_source='native Dynamo common shared-storage tier lookup',
        cache_events='synthetic STORAGE events through native ZMQ/event indexer',
        model_and_kv_io='CPU simulation',check=result,routing_score=score,
        event_hit_fraction=args.event_hit_fraction,
        mismatch_requests=len({e['request_id'] for e in mismatches}),
        pre_score=pre_score))
    print(json.dumps(dict(status='passed',requests=8,score=score)))


if __name__ == '__main__':
    main()
