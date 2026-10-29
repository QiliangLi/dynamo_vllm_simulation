"""Two real workers; opt-in scheduler admission, heavy contention and light reads.

This is a functional smoke, not a performance comparison or a Mooncake replay.
Use an empty output directory. The normal runner owns and cleans its processes.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONHASHSEED'] = '0'  # Verification must use the same block-hash root as workers.
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from run_tfs_stack import run_one, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--kv-mode',choices=['bulk','layer_pipeline'],default='layer_pipeline')
    parser.add_argument('--transport',choices=['dynamo','tcp','unix'],default='dynamo')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    cfg = json.loads((ROOT/'configs/tfs_stack_prefill.json').read_text())
    cfg.update(workers=2,wall_time_scale=10,router_mode='round-robin',kv_transfer_mode=args.kv_mode)
    cfg['admission'].update(placement='scheduler',classification='arrival',retry_interval_wall_s=.002,
                            rpc_transport=args.transport)
    cfg['storage'].update(disks=1,disk_Bps=120e9,shared_link_Bps=None)
    if args.kv_mode != 'bulk':
        cfg['layerwise'] = dict(layers=78,startup='once')
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(str(ROOT/'configs/model'))
    from sim.request_profile import normalize_config, build_profile
    normalized = normalize_config(cfg)
    rows=[]
    # Distinct cache prefixes. Submit heavy rows first to seed the online threshold.
    for i in range(12):
        heavy = i<8
        count,remote = (8208,8192) if heavy else (512,32 if i<10 else 0)
        motif = tok.encode(' hello world test data time request')
        prefix = tok.encode(' hello'*(i+1)+' world')
        tokens=(prefix+motif*((count+5)//6))[:count]
        assert tok.encode(tok.decode(tokens)) == tokens
        row=dict(id=f'smoke-{i}',arrival_s=0 if heavy else .002,prompt_token_ids=tokens,
                 remote_prefix_tokens=remote,output_tokens=1,class_id='heavy' if heavy else 'light')
        rows.append(row)
    profiles=[build_profile(r,normalized) for r in rows]
    # A cap of one is intentional for exercising rejection, with unchanged physical pool.
    cfg['admission']['reference_bandwidth_Bps']=min(120e9,profiles[0].pressure_Bps)
    trace=out/'trace.jsonl'
    trace.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    write(out/'fixture.json',dict(kind='synthetic_functional_smoke',workers=2,requests=len(rows),
        reference_bandwidth_Bps=cfg['admission']['reference_bandwidth_Bps'],
        physical_bandwidth_Bps=120e9,wall_time_scale=10,rpc_transport=args.transport,
        note='A small quota exercises contention; these timings are not a benefit measurement.'))
    result=run_one(cfg,trace,out/'run',Path(sys.executable),'tfs','first_token',2)
    audit=json.loads((out/'run/read_admission.check.json').read_text())
    assert audit['denied']>0 and audit['grants']>0 and audit['heavy_hints']<len(rows),audit
    assert result['workers']==[0,1] and result['held_heavy_peak']==1,result
    write(out/'report.json',dict(status='pass',audit=audit,check=result))
    print(json.dumps(audit),flush=True)


if __name__=='__main__':
    main()
