"""实际组批v2的真实进程链路验收；仅功能小样本，不衡量TFS收益。"""
import argparse
from copy import deepcopy
import hashlib
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
    args = parser.parse_args()
    if os.environ.get('PYTHONHASHSEED') != '0':
        parser.error('set PYTHONHASHSEED=0 to match all worker block hashes')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT/'configs/mooncake_first_seen_layerwise.json').read_text())
    config.update(workers=2, router_mode='round-robin', max_model_len=2048,
                  num_blocks=8193, block_size=16, max_num_batched_tokens=256,
                  wall_time_scale=1, policy='priority', request_ordering='edf')
    config['request_profile']['profile_token_budget'] = 256
    config['admission'].update(aging_enabled=False, feedback_delay_s=0, rpc_transport='unix')
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(ROOT/'configs/model'))
    rng = random.Random(20261028)
    rows = []
    shapes = [(512,16),(128,128),(0,64),(64,384)]*2
    for i,(h,u) in enumerate(shapes):
        tokens = [rng.choice([1332,995,2581,640,1366,23748]) for _ in range(h+u)]
        assert tokenizer.encode(tokenizer.decode(tokens)) == tokens
        rows.append(dict(id='batch-v2-'+str(i),arrival_s=0.,prompt_token_ids=tokens,
            output_tokens=1,remote_prefix_tokens=h,hit_tokens=h,u_tokens=u,
            full_hit_adjusted=False,cache_model='first_seen',class_id=str(i%4)))
    trace = out/'trace.jsonl'
    trace.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    results = []
    for kv,admission in [('bulk','immediate'),('layer_pipeline','tfs')]:
        c = deepcopy(config)
        c['kv_transfer_mode'] = kv
        if admission == 'tfs':
            c['admission'].update(placement='scheduler',classification='arrival')
        folder = out/(kv+'_'+admission)
        check = run_one(c,trace,folder,Path(sys.executable),admission,'first_token',2)
        timings = [json.loads(line)['timing'] for path in folder.glob('worker*.steps.jsonl')
                   for line in path.read_text().splitlines()
                   if json.loads(line).get('num_scheduled_tokens')]
        assert check['workers'] == [0,1]
        assert check['actual_submitted_read_bytes'] == sum(h for h,u in shapes)*109824
        assert all(t['batch_time_model']=='glm5_actual_batch_work_v2' for t in timings)
        assert all((t['reference_tp'],t['reference_dp'],t['reference_ep'])==(1,8,8) for t in timings)
        assert all(t['real_prefill_tokens']==t['padded_prefill_tokens'] for t in timings)
        assert any(t['max_input_length']>512 for t in timings)
        from check_prefill_formula import check as check_formula
        formula = check_formula(sorted(folder.glob('worker*.steps.jsonl')))
        row = dict(kv=kv,admission=admission,status='pass',requests=len(rows),
            steps=len(timings),batched_steps=sum(t['bs']>1 for t in timings),
            mixed_steps=sum(len(set(map(tuple,t['shapes'])))>1 for t in timings),
            formula_max_error_s=formula['max_abs_error_s'],check=check)
        results.append(row)
        write(out/'report.json',dict(status='running',cases=results))
    assert any(r['batched_steps'] for r in results), 'no actual native batch observed'
    sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
             for folder in ('sim','scripts') for p in (ROOT/folder).rglob('*.py')}
    write(out/'report.json',dict(status='pass',cases=results,source_sha256=sources,
        boundary='Synthetic two-worker functionality, not a performance or hardware calibration result.'))
    print(json.dumps(results,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
