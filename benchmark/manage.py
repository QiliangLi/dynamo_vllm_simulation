"""校验、导出和索引标准命名的benchmark文件夹；仅使用Python标准库。"""
import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parent
NAME=re.compile(r'([a-z0-9]+)_([a-z0-9]+)_([1-9][0-9]*)_([0-9]+(?:\.[0-9]+)?)s_([1-9][0-9]*)_([0-9]+(?:\.[0-9]+)?)GBps')
read=lambda p:json.loads(p.read_text(encoding='utf-8'))

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def manifests():
    result=[]
    for folder in sorted(ROOT.iterdir()):
        if not folder.is_dir() or folder.name.startswith(('.', '__')):continue
        if not NAME.fullmatch(folder.name):raise ValueError(f'Invalid benchmark name: {folder.name}')
        m=read(folder/'manifest.json')
        if m['case']!=folder.name:raise ValueError(f'Manifest name differs: {folder.name}')
        result.append(m)
    return result

def case_names(supported_only=False):
    return tuple(m['case'] for m in manifests() if not supported_only or m['portable_runner_status']=='supported')

def resolve_case(name):
    matches=[m['case'] for m in manifests() if name==m['case'] or name in m.get('aliases',[])]
    if len(matches)!=1:raise ValueError(f'Unknown or ambiguous benchmark: {name}')
    return matches[0]

def verify_case(name):
    name=resolve_case(name);folder=ROOT/name;m=read(folder/'manifest.json');cfg=read(folder/'config.json')
    source,feature,count,window,workers,bandwidth=NAME.fullmatch(name).groups()
    count=int(count);window=float(window);workers=int(workers);bandwidth=float(bandwidth)*1e9
    assert window>0 and bandwidth>0
    assert (source,feature)==(m['source'],m['feature'])
    assert count==m['requests'] and workers==m['workers']==cfg['workers']
    assert window==m['arrival_window_s'] and bandwidth==m['shared_storage_Bps']==cfg['storage']['shared_link_Bps']
    assert cfg['storage']['disks']*cfg['storage']['disk_Bps']>=bandwidth
    assert cfg['admission']['reference_bandwidth_Bps']==bandwidth
    assert cfg['wall_time_scale']==m['K']==1 and cfg['block_size']==m['block_size']
    assert not cfg.get('routing_accounting',{}).get('python_path')
    assert m['portable_runner_status'] in ('supported','export_only')
    assert {'trace.jsonl.gz','requests.csv','config.json','config.historical.json'}<=set(m['files_sha256'])
    for file,expected in m['files_sha256'].items():
        assert Path(file).name==file and not file.startswith('.')
        assert sha(folder/file)==expected,(name,file)
    if (folder/'run_contract.json').exists():
        c=read(folder/'run_contract.json')
        assert c['portable_runner_status']==m['portable_runner_status']
        assert c['formal_requests']==count and c['K']==m['K'] and c['arms']['direct']==cfg
    if m['portable_runner_status']=='supported':
        assert m['runner_profile']=='mooncake360_v1'
        assert (count,window,workers)==(360,4,12)
        assert cfg['compute']['batch_time_model']=='glm5_actual_batch_work_v2'
    with (folder/'requests.csv').open(encoding='utf-8',newline='') as f:table=list(csv.DictReader(f))
    digest=hashlib.sha256();totals=Counter();classes=Counter();arrivals=[];timestamps=[];max_input=0
    pattern=m.get('token_pattern');mapping=m.get('arrival_mapping')
    if mapping:
        assert mapping['mode']=='scale_from_origin' and mapping['source_time_unit']=='seconds'
        assert mapping['target_origin_s']==0 and mapping['target_last_s']==window
        assert mapping['source_last_s']>mapping['source_origin_s']>=0
    with gzip.open(folder/'trace.jsonl.gz','rb') as stream:
        for i,line in enumerate(stream):
            digest.update(line);r=json.loads(line);s=table[i];tokens=r['prompt_token_ids']
            n,h,u=len(tokens),r['hit_tokens'],r['u_tokens'];block=cfg['block_size']
            assert all(type(t) is int and t>=0 for t in tokens)
            assert type(h) is int and type(u) is int and n==h+u and 0<=h<n and 1<=u<=n and h%block==0
            assert r['remote_prefix_tokens']==h and r['output_tokens']==1
            assert r['id']==s['id'] and r['class_id']==s['class_id']
            assert (n,h,u,h*cfg['storage']['kv_bytes_per_token'])==tuple(int(s[k]) for k in ('input_tokens','hit_tokens','new_tokens','read_bytes'))
            assert math.isfinite(r['arrival_s']) and r['arrival_s']==float(s['arrival_s'])
            if pattern:assert tokens==(pattern*((n+len(pattern)-1)//len(pattern)))[:n]
            if m['allocation_scope']=='per_request':assert h==(n*96//100)//block*block
            elif m['allocation_scope']=='global':
                assert (h==0)==(r['class_id']=='C')
                if r['class_id']=='A':assert u==(n%block or block)
                if r['class_id']=='D':assert 2033<=u<=2048
            elif m['allocation_scope']=='full_history_prefix_hit':
                src=r['agentx_source']
                assert src['global_line']==int(s['source_line'])==m['source_first_line']+i
                assert src['timestamp_ms']==int(s['source_timestamp'])
                assert src['prefix_hit_tokens']==int(s['source_prefix_hit_tokens'])
                assert h==min(src['prefix_hit_tokens'],(n-1)//block*block)
            elif m['allocation_scope']=='synthetic_abc':
                shape=m['class_shapes'][r['class_id']]
                assert (h,u)==(shape['hit_tokens'],shape['new_tokens'])
                assert r['original_seed416_arrival_s']==float(s['source_timestamp'])
            totals.update(requests=1,input_tokens=n,hit_tokens=h,new_tokens=u,read_bytes=h*cfg['storage']['kv_bytes_per_token'])
            classes[r['class_id']]+=1;arrivals.append(r['arrival_s'])
            timestamps.append(float(s['source_timestamp']) if mapping else int(s['source_timestamp']));max_input=max(max_input,n)
    assert len(table)==totals['requests']==count and len({r['id'] for r in table})==count
    assert digest.hexdigest()==m['original_trace_sha256']
    assert all(totals[k]==m[k] for k in totals) and dict(classes)==m['class_counts']
    assert arrivals[-1]==window and all(a<=b for a,b in zip(arrivals,arrivals[1:]))
    assert timestamps[-1]>timestamps[0]
    if mapping:
        assert 0<=arrivals[0]<window and timestamps[-1]==mapping['source_last_s']
        for a,t in zip(arrivals,timestamps):
            assert math.isfinite(t) and t>=mapping['source_origin_s']
            expected=window*(t-mapping['source_origin_s'])/(mapping['source_last_s']-mapping['source_origin_s'])
            assert math.isclose(a,expected,rel_tol=0,abs_tol=1e-12)
    else:
        assert arrivals[0]==0
        for a,t in zip(arrivals,timestamps):assert math.isclose(a,window*(t-timestamps[0])/(timestamps[-1]-timestamps[0]),abs_tol=1e-12)
    assert cfg['max_model_len']>=max_input+1
    return dict(case=name,**totals,class_counts=dict(classes),original_trace_match=True,arrival_window_s=window,
                shared_storage_Bps=bandwidth,portable_runner_status=m['portable_runner_status'])

def verify():
    names=case_names();assert names,'No benchmarks'
    aliases=[a for m in manifests() for a in m.get('aliases',[])]
    assert len(set(aliases))==len(aliases) and not set(aliases)&set(names)
    cases=[verify_case(n) for n in names]
    return dict(status='passed',requests_checked=sum(c['requests'] for c in cases),K=1,cases=cases)

def export(case,output,dynamo_python=None,quiet=False):
    case=resolve_case(case);verify_case(case);output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    source=ROOT/case;m=read(source/'manifest.json')
    with gzip.open(source/'trace.jsonl.gz','rb') as f,(output/'trace.jsonl').open('wb') as g:shutil.copyfileobj(f,g)
    assert sha(output/'trace.jsonl')==m['original_trace_sha256']
    cfg=read(source/'config.json')
    if dynamo_python:cfg['routing_accounting']['python_path']=str(Path(dynamo_python).resolve())
    (output/'config.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    shutil.copy2(source/'manifest.json',output/'manifest.json')
    if (source/'run_contract.json').exists():shutil.copy2(source/'run_contract.json',output/'run_contract.json')
    result=dict(status='exported',case=case,output=str(output),trace_sha256=sha(output/'trace.jsonl'),
                config_sha256=sha(output/'config.json'),portable_runner_status=m['portable_runner_status'])
    if not quiet:print(json.dumps(result,ensure_ascii=False,indent=2))
    return result

def catalog_text():
    lines=['# Benchmark目录','','本表从各文件夹的 manifest.json 生成。新增负载后运行 python benchmark/manage.py catalog 更新。','',
           '| Benchmark（点击查看说明） | 来源 / 特征 | 请求数 / 窗口 | 实例 / 带宽 | 新计算 token（比例） | 读取 GB | 类别数量 | 计算模型 / 反馈 | 支持状态 |',
           '|---|---|---|---|---|---|---|---|---|']
    for m in manifests():
        cfg=read(ROOT/m['case']/'config.json');classes='/'.join(f'{k}:{v}' for k,v in sorted(m['class_counts'].items()))
        model=cfg['compute'].get('batch_time_model',cfg['compute']['mode'])
        status='可运行' if m['portable_runner_status']=='supported' else '仅校验、导出'
        lines.append(f"| [{m['case']}]({m['case']}/README.md) | {m['source']} / {m['feature']} | {m['requests']} / {m['arrival_window_s']:g}s | {m['workers']} / {m['shared_storage_Bps']/1e9:g} GB/s | {m['new_tokens']}（{m['new_pct']:.6f}%） | {m['read_bytes']/1e9:.6f} | {classes} | {model} / {m['execution_feedback']} | {status} |")
    lines+=['','## 来源与构造说明','']
    for m in manifests():
        source=m.get('source_dataset',m.get('source_file',m['source']))
        lines.append(f"- **{m['case']}**：{m['description']} 来源：{source}，原行号 {m['source_first_line']}—{m['source_last_line']}。输入 SHA256：{m['original_trace_sha256']}。")
    lines+=['','## 附录：字段与类别','',
        '- 名称顺序：数据来源_数据特征_请求总数_发送窗口_计算实例数_存储带宽。时间用 s；GBps 表示十进制 GB/s，1 GB = 10^9 B。带宽指单共享池，不是每实例独享带宽。',
        '- random：未按 ABC/ABCD 人为分配请求类别。不表示随机打乱、无采样或命中未处理。两份 random 的命中规则不同，见来源说明及各自 manifest。',
        '- mooncake 来源的 abc / abcd：总 input 的约 4% 为全局新算预算。A 重读轻计算（新算 1—16 token）；B 轻读重计算（承接剩余额度）；C 零读（短请求全部重算）；D 重读重计算（最长 20 条各新算 2033—2046 token）。按长度选类别后恢复原 trace 顺序，不按类别重排。',
        '- synthetic 来源的 abc：固定三种请求形状，A 重读轻计算、B 较轻读且计算更重、C 零读且计算时间接近 A。h/u 取自 manifest 的 class_shapes，不使用 Mooncake 全局 4% 规则；保留 seed416 原到达顺序。',
        '- 表中 A/B/C/D 是人工或离线展示标签，不是运行期 TFS P75 轻重分类。random 的 A/B 不按 ABC 构造解释。',
        '- 新计算比例 = sum(u)/sum(input)。逐请求约 4% 与整批全局约 4% 不等价；block16 影响取整。h 为有效远端命中 token，u 为新计算 token，input=h+u。',
        '- 默认窗口是首个计划到达到最后一个计划到达的间隔；manifest 有 arrival_mapping 时按其时间原点到末次计划到达定义，保留首请求之前的间隔。实际发送迟到和整轮完成时间另行统计。实例数为逻辑实例数，不等于物理 GPU/NPU 数量。',
        '- config.json 是本负载运行配置，config.historical.json 只用于追溯。模型版本、反馈、block、SLO 以配置及合同为准，不从名称猜测。T0/期限按当前配置重算，不能复用另一模型或带宽下的旧期限。',
        '- 可运行：当前交付运行器支持。仅校验、导出：不得套用其他负载默认条件。AgentX 有 39 条超过 131072 token，最长 300480，涉及公式外推。所有计算时间均为解析仿真，不是实机测量。','']
    return '\n'.join(lines)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('list');sub.add_parser('verify');p=sub.add_parser('catalog');p.add_argument('--check',action='store_true')
    p=sub.add_parser('export');p.add_argument('case');p.add_argument('--output',type=Path,required=True);p.add_argument('--dynamo-python',type=Path)
    args=parser.parse_args()
    if args.action=='list':print('\n'.join(case_names()))
    elif args.action=='verify':print(json.dumps(verify(),ensure_ascii=False,indent=2))
    elif args.action=='catalog':
        verify();path=ROOT/'benchmark目录.md'
        if args.check:assert path.read_text(encoding='utf-8')==catalog_text(),'Benchmark table is stale'
        else:path.write_text(catalog_text(),encoding='utf-8',newline='\n')
        print('catalog passed')
    else:export(args.case,args.output,args.dynamo_python)
