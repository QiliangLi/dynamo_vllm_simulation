"""Two-worker real-router smoke; cache events and KV IO are explicit fixtures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from run_tfs_stack import run_one, write


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--routing-python-path', required=True, type=Path)
    args = p.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True,exist_ok=False)
    cfg = json.loads((ROOT/'configs/sa_abc_stack.json').read_text())
    cfg.pop('cache_model',None)
    cfg.update(workers=2,router_mode='kv',request_ordering='edf',policy='priority',
               max_model_len=20000,num_blocks=100000,wall_time_scale=20)
    cfg['admission'].update(placement='scheduler',classification='arrival',
                            profile_source='dynamo_tier',aging_enabled=False)
    cfg['routing_accounting'] = dict(enabled=True,waiting_factor=.1,
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
            kwargs['env'] = dict(kwargs['env'], SIM_TIER_FIXTURE_TRACE=str(trace))
        return original(command, **kwargs)
    with patch('run_tfs_stack.subprocess.Popen',start):
        result = run_one(cfg,trace,out/'run',Path(sys.executable),'tfs','first_token',2)
    state = json.loads((out/'run/admission.final.json').read_text())
    profiles = [e for e in state['events'] if e['event']=='dynamo_tier_profile_resolved']
    assert len(profiles)==8
    assert [state['records'][r['id']]['profile']['estimated_remote_tokens'] for r in rows] == [r['remote_prefix_tokens'] for r in rows]
    from check_routing_accounting import check
    score = check(out/'run',require_discount=True)
    write(out/'validation.json',dict(status='passed',requests=8,workers=2,
        profile_source='native Dynamo selected-worker tier lookup',
        cache_events='synthetic STORAGE events through native ZMQ/event indexer',
        model_and_kv_io='CPU simulation',check=result,routing_score=score))
    print(json.dumps(dict(status='passed',requests=8,score=score)))


if __name__ == '__main__':
    main()
