"""Two native workers exercise partial scores and permit activation end to end."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
os.environ['PYTHONHASHSEED']='0'
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from run_tfs_stack import run_one,write

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--routing-python-path',type=Path,required=True)
    p.add_argument('--factor',type=float,default=.1)
    p.add_argument('--transport',choices=['dynamo','tcp','unix'],default='dynamo')
    p.add_argument('--disabled',action='store_true',help='same patched binary with weighted accounting disabled')
    p.add_argument('--light-only',action='store_true',help='all requests have zero remote prefix and no heavy registration')
    args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    cfg=json.loads((ROOT/'configs/sa_abc_stack.json').read_text())
    cfg.pop('cache_model',None)
    cfg.update(workers=2,max_model_len=20000,num_blocks=100000,wall_time_scale=10)
    cfg['admission'].update(placement='scheduler',classification='arrival',rpc_transport=args.transport)
    cfg['routing_accounting']=dict(enabled=not args.disabled,waiting_factor=args.factor,python_path=str(args.routing_python_path.resolve()))
    from transformers import AutoTokenizer
    tok=AutoTokenizer.from_pretrained(str(ROOT/'configs/model'))
    rows=[]
    for i in range(12):
        heavy=not args.light_only and i<8
        count,remote=(8208,8192) if heavy else (512,32 if i<10 else 0)
        if args.light_only:remote=0
        motif=tok.encode(' hello world test data time request')
        prefix=tok.encode(' hello'*(i+1)+' world')
        tokens=(prefix+motif*((count+5)//6))[:count]
        assert tok.encode(tok.decode(tokens))==tokens
        rows.append(dict(id=f'score-smoke-{i}',arrival_s=0 if heavy else .002,
                         prompt_token_ids=tokens,remote_prefix_tokens=remote,output_tokens=1,
                         class_id='heavy' if heavy else 'light'))
    from sim.request_profile import normalize_config,build_profile
    normalized=normalize_config(cfg)
    profiles=[build_profile(r,normalized) for r in rows]
    if profiles[0].pressure_Bps>0:
        cfg['admission']['reference_bandwidth_Bps']=min(120e9,profiles[0].pressure_Bps)
    trace=out/'trace.jsonl';trace.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    write(out/'fixture.json',dict(kind='functional smoke, not benefit benchmark',requests=12,workers=2,factor=args.factor))
    result=run_one(cfg,trace,out/'run',Path(sys.executable),'tfs','first_token',2)
    if args.disabled:
        state=json.loads((out/'run/admission.final.json').read_text())
        assert state['routing_score'] is None and not (out/'run/routing.score.identity.json').exists()
        audit=dict(status='passed',enabled=False,same_binary_overlay=True,requests=12)
    else:
        from check_routing_accounting import check
        audit=check(out/'run',require_discount=not args.light_only)
    write(out/'report.json',dict(status='passed',check=result,routing_score=audit))
    print(json.dumps(audit))

if __name__=='__main__':main()
