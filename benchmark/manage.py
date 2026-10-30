"""校验冻结benchmark，或导出JSONL与配置；导出不代表运行器兼容。"""
import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parent
MOONCAKE_CASES=('per_request_4pct','global4_abc','global4_abcd')
AGENTX_CASE='agentx360_12s_180gb'
CASES=(*MOONCAKE_CASES,AGENTX_CASE)
read=lambda p:json.loads(p.read_text(encoding='utf-8'))


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def verify_mooncake():
    results=[];common=None
    for name in MOONCAKE_CASES:
        folder=ROOT/name;m=read(folder/'manifest.json')
        for file,expected in m['files_sha256'].items():
            assert sha(folder/file)==expected,(name,file)
        cfg=read(folder/'config.json')
        assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==90e9
        assert cfg['admission']['reference_bandwidth_Bps']==90e9
        assert cfg['wall_time_scale']==1 and cfg['block_size']==16
        assert cfg['workers']==12 and not cfg['routing_accounting'].get('python_path')
        if common is None:common=cfg
        else:assert common==cfg,'Three cases must use the same comparison config'
        with (folder/'requests.csv').open(encoding='utf-8',newline='') as f:
            table=list(csv.DictReader(f))
        digest=hashlib.sha256();count=n_total=h_total=u_total=0;classes=Counter();identities=[]
        with gzip.open(folder/'trace.jsonl.gz','rb') as stream:
            for i,line in enumerate(stream):
                digest.update(line);r=json.loads(line);s=table[i]
                n=len(r['prompt_token_ids']);h=r['hit_tokens'];u=r['u_tokens']
                assert n==h+u and h%16==0 and 1<=u<=n
                assert r['remote_prefix_tokens']==h and r['output_tokens']==1
                assert r['id']==s['id']==f'conversation:L{i+1}'
                assert (n,h,u,h*109824)==tuple(int(s[k]) for k in ('input_tokens','hit_tokens','new_tokens','read_bytes'))
                assert r['arrival_s']==float(s['arrival_s']) and r['class_id']==s['class_id']
                assert r['prompt_token_ids']==([1332,995,2581,640,1366,23748]*((n+5)//6))[:n]
                if name=='per_request_4pct':assert h==(n*96//100)//16*16
                else:
                    assert (h==0)==(r['class_id']=='C')
                    if r['class_id']=='A':assert u==n%16 or (n%16==0 and u==16)
                    if r['class_id']=='D':assert 2033<=u<=2048
                count+=1;n_total+=n;h_total+=h;u_total+=u;classes[r['class_id']]+=1
                identities.append((r['id'],n,r['arrival_s'],int(s['source_timestamp'])))
        assert count==len(table)==m['requests']==360
        assert digest.hexdigest()==m['original_trace_sha256']
        assert (n_total,h_total,u_total)==(m['input_tokens'],m['hit_tokens'],m['new_tokens'])
        assert dict(classes)==m['class_counts']
        assert u_total==(207547 if name=='per_request_4pct' else 204699)
        assert identities[0][2]==0 and identities[-1][2]==4
        start,end=identities[0][3],identities[-1][3]
        for _,_,arrival,timestamp in identities:
            assert math.isclose(arrival,(timestamp-start)/(end-start)*4,abs_tol=1e-12)
        if name==MOONCAKE_CASES[0]:reference=identities
        else:assert identities==reference,'Original input and arrival rhythm must be identical'
        results.append(dict(case=name,requests=count,new_tokens=u_total,new_pct=u_total/n_total*100,
            read_bytes=h_total*109824,class_counts=dict(classes),original_trace_match=True))
    assert results[1]['read_bytes']==results[2]['read_bytes']==539571462144
    assert results[1]['class_counts']==dict(A=240,B=90,C=30)
    assert results[2]['class_counts']==dict(A=220,B=90,C=30,D=20)
    return dict(status='passed',requests_checked=1080,shared_storage_Bps=90e9,K=1,cases=results)


def verify_agentx():
    folder=ROOT/AGENTX_CASE;m=read(folder/'manifest.json')
    for file,expected in m['files_sha256'].items():
        assert sha(folder/file)==expected,(AGENTX_CASE,file)
    cfg=read(folder/'config.json');contract=read(folder/'run_contract.json')
    assert cfg==read(folder/'config.historical.json')==contract['arms']['direct']
    assert cfg['workers']==12 and cfg['wall_time_scale']==1 and cfg['block_size']==16
    assert cfg['num_blocks']==30159 and cfg['max_num_seqs']==4 and cfg['max_num_batched_tokens']==8192
    assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==cfg['admission']['reference_bandwidth_Bps']==180e9
    assert cfg['storage']['disks']==1 and cfg['storage']['paths_per_disk']==128
    assert cfg['storage']['kv_bytes_per_token']==109824
    assert cfg['compute']['batch_time_model']=='glm5_direct_sum_v1' and cfg['compute']['launch_saving_fraction']==1
    assert (cfg['compute']['tp'],cfg['compute']['dp'],cfg['compute']['ep'])==(1,8,8)
    assert cfg['request_ordering']=='edf' and not cfg['enable_prefix_caching']
    assert cfg['kv_transfer_mode']=='layer_pipeline' and cfg['layerwise']==dict(layers=78,startup='once')
    assert cfg['request_profile']['slo_multiplier']==4 and cfg['admission']['rpc_transport']=='dynamo'
    assert contract['execution_feedback']=='disabled' and contract['warmup_requests']==72
    assert contract['formal_requests']==360 and contract['formula_max_context_guard']==cfg['max_model_len']==300481
    assert contract['portable_runner_status']==m['portable_runner_status']=='export_only'
    for arm,factor in [('tfs-a01',.1),('tfs-a1',1.)]:
        c=contract['arms'][arm]
        assert c['admission']['mode']=='tfs' and c['admission']['placement']=='scheduler'
        assert c['admission']['classification']=='arrival' and not c['admission']['idle_bypass']
        assert c['routing_accounting']['enabled'] and c['routing_accounting']['waiting_factor']==factor
        assert not c['routing_accounting'].get('python_path')
        # Only algorithm selection and waiting score differ from the frozen direct input.
        normalized=json.loads(json.dumps(c))
        normalized['admission'].update(mode='immediate',placement='gateway',classification='dynamic')
        normalized['routing_accounting'].update(enabled=False,waiting_factor=.1)
        assert normalized==cfg
    with (folder/'requests.csv').open(encoding='utf-8',newline='') as f:table=list(csv.DictReader(f))
    digest=hashlib.sha256();totals=Counter();classes=Counter();identities=[];max_input=0
    with gzip.open(folder/'trace.jsonl.gz','rb') as stream:
        for i,line in enumerate(stream):
            digest.update(line);r=json.loads(line);s=table[i];source=r['agentx_source']
            n=len(r['prompt_token_ids']);h=r['hit_tokens'];u=r['u_tokens'];global_line=2608+i
            assert r['id']==s['id']==f'agentx:L{global_line}'
            assert source['global_line']==int(s['source_line'])==global_line
            assert source['timestamp_ms']==int(s['source_timestamp'])
            assert source['prefix_hit_tokens']==int(s['source_prefix_hit_tokens'])
            assert 0<=source['prefix_hit_tokens']<=n
            assert h==min(source['prefix_hit_tokens'],(n-1)//16*16) and h%16==0
            assert n==h+u and 1<=u<=n and r['remote_prefix_tokens']==h and r['output_tokens']==1
            assert (n,h,u,h*109824)==tuple(int(s[k]) for k in ('input_tokens','hit_tokens','new_tokens','read_bytes'))
            assert r['arrival_s']==float(s['arrival_s']) and r['class_id']==s['class_id']
            assert r['prompt_token_ids']==([1332,995,2581,640,1366,23748]*((n+5)//6))[:n]
            totals.update(requests=1,input_tokens=n,hit_tokens=h,new_tokens=u,read_bytes=h*109824,long_context_requests=int(n>131072))
            classes[r['class_id']]+=1;max_input=max(max_input,n)
            identities.append((r['arrival_s'],source['timestamp_ms']))
    assert len(table)==totals['requests']==m['requests']==360
    assert digest.hexdigest()==m['original_trace_sha256']
    assert all(totals[key]==m[key] for key in totals)
    assert dict(classes)==m['class_counts']==dict(A=90,B=270)
    assert max_input==m['max_input_tokens']==300480 and totals['long_context_requests']==39
    assert identities[0][0]==0 and identities[-1][0]==12
    start,end=identities[0][1],identities[-1][1]
    assert all(a<=b for a,b in zip(identities,identities[1:]))
    for arrival,timestamp in identities:
        assert math.isclose(arrival,12*(timestamp-start)/(end-start),abs_tol=1e-12)
    return dict(case=AGENTX_CASE,**totals,class_counts=dict(classes),original_trace_match=True,
                arrival_window_s=12,shared_storage_Bps=180e9,portable_runner_status='export_only')


def verify():
    result=verify_mooncake();agentx=verify_agentx()
    result['cases'].append(agentx);result['requests_checked']+=agentx['requests']
    result.pop('shared_storage_Bps')
    result['storage_Bps_by_case']={name:90e9 for name in MOONCAKE_CASES}
    result['storage_Bps_by_case'][AGENTX_CASE]=180e9
    catalog=read(ROOT/'catalog.json')
    assert tuple(c['case'] for c in catalog['cases'])==CASES
    for c,r in zip(catalog['cases'],result['cases']):
        for key in ('requests','new_tokens','read_bytes','class_counts'):assert c[key]==r[key]
    return result


def export(case,output,dynamo_python=None):
    verify()
    output=output.resolve();output.mkdir(parents=True,exist_ok=False)
    source=ROOT/case
    with gzip.open(source/'trace.jsonl.gz','rb') as f,(output/'trace.jsonl').open('wb') as g:
        shutil.copyfileobj(f,g)
    assert sha(output/'trace.jsonl')==read(source/'manifest.json')['original_trace_sha256']
    cfg=read(source/'config.json')
    if dynamo_python:cfg['routing_accounting']['python_path']=str(dynamo_python.resolve())
    (output/'config.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if (source/'run_contract.json').exists():shutil.copy2(source/'run_contract.json',output/'run_contract.json')
    manifest=read(source/'manifest.json')
    print(json.dumps(dict(status='exported',case=case,output=str(output),
        trace_sha256=sha(output/'trace.jsonl'),config_sha256=sha(output/'config.json'),
        shared_storage_Bps=cfg['storage']['shared_link_Bps'],K=1,
        portable_runner_status=manifest.get('portable_runner_status','supported')),
        ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('verify')
    p=sub.add_parser('export');p.add_argument('case',choices=CASES)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--dynamo-python',type=Path,help='Optional local weighted Dynamo Python overlay')
    args=parser.parse_args()
    if args.action=='verify':print(json.dumps(verify(),ensure_ascii=False,indent=2))
    else:export(args.case,args.output,args.dynamo_python)
