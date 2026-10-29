"""Build exclusive request ledgers plus explicitly nested diagnostics."""
from collections import Counter,defaultdict
import csv
import json
import math
import mmap
from pathlib import Path
import re
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from check_fine_trace import check,key


def read(path):return json.loads(path.read_text())
def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
def csvout(path,rows):
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def fields(path,wanted):
    result={}
    with path.open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
        matches=list(re.finditer(rb'^  "([A-Za-z0-9_]+)":',data,re.M))
        for i,m in enumerate(matches):
            name=m.group(1).decode()
            if name in wanted:
                end=matches[i+1].start() if i+1<len(matches) else data.rfind(b'\n}')
                result[name]=json.loads(data[m.end():end].strip().rstrip(b','))
    assert set(result)==set(wanted)
    return result


STAGES=['central','before_forward','http_dynamo','worker_prepare','async_prepare','ipc_encode',
    'ipc_transport','ipc_decode','decode_to_preprocess','core_preprocess','preprocess_to_queue',
    'core_input_queue','core_dispatch','engine_wait','schedule','schedule_to_executor',
    'executor_prepare','first_layer_submit','layer_wait_physical','layer_wait_after_ready',
    'modeled_compute','compute_overrun','layer_control','between_chunks','output_core_update','output_core_worker','output_worker_gateway']


def aggregate(rows):
    result={}
    for cls in ('all','A','B','C'):
        rr=[r for r in rows if cls=='all' or r['class_id']==cls]
        ttfts=[r['ttft_ms'] for r in rr]
        result[cls]=dict(requests=len(rr),mean_ttft_ms=statistics.mean(ttfts),
            p50_ttft_ms=statistics.median(ttfts),p95_ttft_ms=quantile(ttfts,.95),
            slo=sum(r['slo'] for r in rr)/len(rr),
            stages_mean_ms={s:statistics.mean(r[s+'_ms'] for r in rr) for s in STAGES},
            max_batch_histogram=dict(Counter(r['max_batch'] for r in rr)),
            chunk_count_histogram=dict(Counter(r['steps'] for r in rr)))
    return result


def quantile(items,p):
    ss=sorted(items);index=(len(ss)-1)*p;a=int(index);b=min(a+1,len(ss)-1)
    return ss[a]+(ss[b]-ss[a])*(index-a)


def native(folder,out):
    validation=check(folder);write(folder/'fine_trace_check.json',validation)
    cfg=read(folder/'config.json');K=cfg['wall_time_scale'];unit=1e-6/K
    state=read(folder/'admission.final.json');records=state['records']
    mapping={key(e['engine_uuid']):e['id'] for e in state['events'] if e['event']=='request_correlated'}
    rows=[];byrequest=defaultdict(lambda:defaultdict(list));byworker=defaultdict(list)
    pid_info={int(p.stem.split('-')[1]):read(p) for p in (folder/'fine_trace').glob('ready-*.json')}
    for p in (folder/'fine_trace').glob('trace-*.jsonl'):
        pid=int(p.stem.split('-')[1]);w=pid_info[pid].get('worker')
        for line in p.read_text().splitlines():
            event=json.loads(line);event['pid']=pid;rows.append(event)
            if w is not None:byworker[int(w)].append(event)
            ids=([event['rid']] if event.get('rid') else [])+event.get('ids',[])+event.get('req_ids',[])
            for ident in set(ids):
                k=key(ident)
                if k in mapping:byrequest[mapping[k]][event['event']].append(event)
    origin=next(r['origin_s'] for r in rows if r['event']=='gateway_origin')
    forward={e['id']:e['t'] for e in state['events'] if e['event']=='http_forwarded'}
    store=fields(folder/'storage.final.json',['t0_monotonic_s','sa_flow_history','storage_intervals'])
    flows={(f['worker'],f['step'],f['layer']):f for f in store['sa_flow_history']}
    steps=[];stepmap={}
    for worker,events in byworker.items():
        for ex in [e for e in events if e['event']=='executor']:
            a,b=ex['start_ns'],ex['end_ns'];stepid=ex['step']
            ids=[mapping[key(s)] for s in ex['ids']]
            within=[e for e in events if a<=e['start_ns']<=e['end_ns']<=b]
            begin=sorted([e for e in within if e['event']=='layer_compute_start'],key=lambda e:e['layer'])
            end=sorted([e for e in within if e['event']=='layer_compute_complete'],key=lambda e:e['layer'])
            assert len(begin)==len(end)==8
            submits=[e for e in within if e['event']=='layer_submit' and e['layer']==0]
            assert len(submits)==1
            parts={s:0. for s in STAGES}
            parts['executor_prepare']=(submits[0]['start_ns']-a)*unit
            parts['first_layer_submit']=(submits[0]['end_ns']-submits[0]['start_ns'])*unit
            segments=[]
            for start,stop in zip(begin,end):
                nominal=start['duration_model_s']*1000
                actual=(stop['end_ns']-start['end_ns'])*unit
                assert actual+1e-4>=nominal
                parts['modeled_compute']+=nominal
                parts['compute_overrun']+=actual-nominal
                segments.append(dict(kind='compute',start_s=(start['end_ns']/1e9-origin)/K,
                    end_s=(start['end_ns']/1e9-origin)/K+nominal/1000))
            for wait in [e for e in within if e['event']=='layer_wait']:
                flow=flows.get((worker,stepid,wait['layer']))
                complete=(store['t0_monotonic_s']+K*flow['completed_s'])*1e9 if flow else wait['start_ns']
                physical=max(0.,min(wait['end_ns'],complete)-wait['start_ns'])*unit
                total=(wait['end_ns']-wait['start_ns'])*unit
                parts['layer_wait_physical']+=physical
                parts['layer_wait_after_ready']+=total-physical
                if physical:
                    segments.append(dict(kind='io',start_s=(wait['start_ns']/1e9-origin)/K,
                        end_s=(wait['start_ns']/1e9-origin)/K+physical/1000))
            duration=(b-a)*unit
            parts['layer_control']=duration-sum(parts.values())
            assert parts['layer_control']>=-1e-4,parts
            relevant=[e for e in events if e['event']=='schedule' and e['ids']==ex['ids'] and e['end_ns']<=a]
            schedule=max(relevant,key=lambda e:e['end_ns'])
            steps.append(dict(worker=worker,step=stepid,ids=ids,batch_size=len(ids),
                classes='+'.join(sorted({records[s]['profile']['class_id'] for s in ids})),
                start_s=(a/1e9-origin)/K,end_s=(b/1e9-origin)/K,
                start_ns=a,end_ns=b,schedule=schedule,parts=parts,segments=segments))
            stepmap[worker,stepid]=steps[-1]
    output=[]
    for source,record in records.items():
        ev=byrequest[source]
        def first(name):return min(ev[name],key=lambda e:e['start_ns'])
        def point(name,field='start_ns'):return first(name)[field]
        s=sorted([e for e in steps if source in e['ids']],key=lambda e:e['start_ns'])
        p=record['profile'];parts={name:0. for name in STAGES}
        parts['central']=(record['submitted_s']-p['arrival_s'])*1000
        parts['before_forward']=(forward[source]-record['submitted_s'])*1000
        parts['http_dynamo']=((point('worker_enter')/1e9-origin)/K-forward[source])*1000
        chain=[('worker_prepare',point('worker_enter'),point('async_add_enter')),
            ('async_prepare',point('async_add_enter'),point('ipc_encode')),
            ('ipc_encode',point('ipc_encode'),point('ipc_send_ready')),
            ('ipc_transport',point('ipc_send_ready'),point('ipc_decode')),
            ('ipc_decode',point('ipc_decode'),point('ipc_decode','end_ns')),
            ('decode_to_preprocess',point('ipc_decode','end_ns'),point('core_preprocess')),
            ('core_preprocess',point('core_preprocess'),point('core_preprocess','end_ns')),
            ('preprocess_to_queue',point('core_preprocess','end_ns'),point('core_queue_put')),
            ('core_input_queue',point('core_queue_put'),point('core_queue_get')),
            ('core_dispatch',point('core_queue_get'),point('scheduler_add','end_ns')),
            ('engine_wait',point('scheduler_add','end_ns'),s[0]['schedule']['start_ns'])]
        for name,a,b in chain:
            assert b>=a,(source,name,a,b)
            parts[name]=(b-a)*unit
        for step in s:
            sch=step['schedule']
            parts['schedule']+=(sch['end_ns']-sch['start_ns'])*unit
            parts['schedule_to_executor']+=(step['start_ns']-sch['end_ns'])*unit
            for k,v in step['parts'].items():parts[k]+=v
        parts['between_chunks']=sum((v['schedule']['start_ns']-u['end_ns'])*unit for u,v in zip(s,s[1:]))
        yielded=point('worker_first_yield')
        updates=[e for e in ev['scheduler_update'] if e['start_ns']>=s[-1]['end_ns']]
        update=min(updates,key=lambda e:e['start_ns'])
        parts['output_core_update']=(update['end_ns']-s[-1]['end_ns'])*unit
        parts['output_core_worker']=(yielded-update['end_ns'])*unit
        parts['output_worker_gateway']=(record['first_token_s']-(yielded/1e9-origin)/K)*1000
        ttft=(record['first_token_s']-p['arrival_s'])*1000
        error=abs(sum(parts.values())-ttft)
        assert error<1e-6,(source,error)
        assert min(parts.values())>-1e-4,(source,parts)
        schedule_rpcs=[e for e in ev['storage_rpc'] if any(
            step['schedule']['start_ns']<=e['start_ns']<=e['end_ns']<=step['schedule']['end_ns'] for step in s)]
        output.append(dict(id=source,class_id=p['class_id'],worker=record['worker_id'],
            arrival_s=p['arrival_s'],first_token_s=record['first_token_s'],ttft_ms=ttft,
            deadline_ms=(p['deadline_s']-p['arrival_s'])*1000,slo=int(record['first_token_s']<=p['deadline_s']),
            max_batch=max(e['batch_size'] for e in s),steps=len(s),
            **{name+'_ms':parts[name] for name in STAGES},ledger_error_ms=error,
            input_process_nested_ms=sum((x['end_ns']-x['start_ns'])*unit for x in ev['input_process']),
            output_register_nested_ms=sum((x['end_ns']-x['start_ns'])*unit for x in ev['output_register']),
            kv_allocate_nested_ms=sum((x['end_ns']-x['start_ns'])*unit for x in ev['kv_allocate']),
            schedule_storage_rpc_nested_ms=sum((x['end_ns']-x['start_ns'])*unit for x in schedule_rpcs),
            schedule_rpc_encode_nested_ms=sum((x['encoded_ns']-x['start_ns'])*unit for x in schedule_rpcs),
            schedule_rpc_send_nested_ms=sum((x['sent_ns']-x['encoded_ns'])*unit for x in schedule_rpcs),
            schedule_rpc_response_nested_ms=sum((x['got_ns']-x['sent_ns'])*unit for x in schedule_rpcs),
            schedule_rpc_decode_nested_ms=sum((x['end_ns']-x['got_ns'])*unit for x in schedule_rpcs),
            feedback_rpc_nested_ms=sum((x['end_ns']-x['start_ns'])*unit for x in ev['feedback_rpc'])))
    rpc=[]
    for event in rows:
        if event['event']=='storage_rpc':
            rpc.append(dict(kind='client',op=event['op'],total_ms=(event['end_ns']-event['start_ns'])*unit,
                encode_ms=(event['encoded_ns']-event['start_ns'])*unit,
                send_ms=(event['sent_ns']-event['encoded_ns'])*unit,
                response_wait_ms=(event['got_ns']-event['sent_ns'])*unit,
                decode_ms=(event['end_ns']-event['got_ns'])*unit,lock_and_decode_ms=0.,process_ms=0.,reply_ms=0.))
        elif event['event']=='storage_service':
            rpc.append(dict(kind='server',op=event['op'],total_ms=(event['end_ns']-event['start_ns'])*unit,
                encode_ms=0.,send_ms=0.,response_wait_ms=0.,decode_ms=0.,
                lock_and_decode_ms=(event['locked_ns']-event['start_ns'])*unit,
                process_ms=(event['processed_ns']-event['locked_ns'])*unit,
                reply_ms=(event['end_ns']-event['processed_ns'])*unit))
    summary=dict(family='native',model_time_scale=K,groups=aggregate(output),
        makespan_ms=(max(r['first_token_s'] for r in output)-min(r['arrival_s'] for r in output))*1000,
        batch_histogram=dict(Counter(s['batch_size'] for s in steps)),steps=len(steps),
        max_ledger_error_ms=max(r['ledger_error_ms'] for r in output),trace_validation=validation)
    csvout(out/'requests.csv',output);write(out/'steps.json',steps);write(out/'summary.json',summary)
    csvout(out/'rpc.csv',rpc)
    # Compact intervals retain exact integration inputs without duplicating block hashes.
    intervals=[]
    for interval in store['storage_intervals']:
        intervals.append(interval)
    write(out/'storage_intervals.json',dict(origin_delta_s=(store['t0_monotonic_s']-origin)/K,intervals=intervals))
    nested=[]
    for name in ('input_process','output_register','kv_allocate','storage_rpc','storage_service','feedback_rpc','layer_sleep'):
        rr=[r for r in rows if r['event']==name]
        nested.append(dict(event=name,count=len(rr),total_model_ms=sum((r['end_ns']-r['start_ns'])*unit for r in rr),
            mean_model_ms=statistics.mean((r['end_ns']-r['start_ns'])*unit for r in rr) if rr else 0.,
            requested_model_ms=sum(r.get('requested_s',0.)*1000/K for r in rr)))
    write(out/'nested.json',nested)
    return summary


def des(folder,out):
    report=read(folder/'report.json');records=report['requests']
    events=[json.loads(s) for s in (folder/'events.jsonl').read_text().splitlines()]
    ends={(e['worker'],e['step_id']):e['t'] for e in events if e['event']=='step_complete'}
    layers=defaultdict(list)
    for e in events:
        if e['event']=='layer_compute_started':layers[e['worker'],e['step_id']].append(e)
    steps=[]
    for e in events:
        if e['event']!='step_started':continue
        ids=list(e['num_scheduled_tokens']);end=ends[e['worker'],e['step_id']]
        compute=8*e['layer_compute_s'];segments=[];previous=e['t']
        for layer in sorted(layers[e['worker'],e['step_id']],key=lambda x:x['t']):
            if layer['t']>previous:segments.append(dict(kind='io',start_s=previous,end_s=layer['t']))
            stop=layer['t']+e['layer_compute_s'];segments.append(dict(kind='compute',start_s=layer['t'],end_s=stop));previous=stop
        assert abs(previous-end)<1e-8
        steps.append(dict(worker=e['worker'],step=e['step_id'],ids=ids,batch_size=len(ids),
            classes='+'.join(sorted({records[r]['class_id'] for r in ids})),start_s=e['t'],end_s=end,
            parts={'modeled_compute':compute*1000,'layer_wait_physical':(end-e['t']-compute)*1000},segments=segments))
    output=[]
    for source,r in records.items():
        ss=sorted([s for s in steps if source in s['ids']],key=lambda s:s['start_s'])
        parts={s:0. for s in STAGES}
        parts['central']=(r['submitted_s']-r['arrival_s'])*1000
        parts['engine_wait']=(ss[0]['start_s']-r['submitted_s'])*1000
        parts['modeled_compute']=sum(s['parts']['modeled_compute'] for s in ss)
        parts['layer_wait_physical']=sum(s['parts']['layer_wait_physical'] for s in ss)
        parts['between_chunks']=sum((b['start_s']-a['end_s'])*1000 for a,b in zip(ss,ss[1:]))
        ttft=(r['first_token_s']-r['arrival_s'])*1000
        error=abs(sum(parts.values())-ttft);assert error<1e-6
        output.append(dict(id=source,class_id=r['class_id'],worker=r['worker'],
            arrival_s=r['arrival_s'],first_token_s=r['first_token_s'],ttft_ms=ttft,
            deadline_ms=(r['deadline_s']-r['arrival_s'])*1000,slo=int(r['first_token_s']<=r['deadline_s']),
            max_batch=max(s['batch_size'] for s in ss),steps=len(ss),
            **{s+'_ms':v for s,v in parts.items()},ledger_error_ms=error))
    summary=dict(family='des',model_time_scale=None,groups=aggregate(output),
        makespan_ms=(max(r['first_token_s'] for r in output)-min(r['arrival_s'] for r in output))*1000,
        batch_histogram=dict(Counter(s['batch_size'] for s in steps)),steps=len(steps),
        max_ledger_error_ms=max(r['ledger_error_ms'] for r in output))
    csvout(out/'requests.csv',output);write(out/'steps.json',steps);write(out/'summary.json',summary)
    write(out/'storage_intervals.json',dict(origin_delta_s=0.,intervals=[e for e in events if e['event']=='storage_interval']))
    return summary


if __name__=='__main__':
    dest=ROOT/'results/seed416_fine_analysis_20261026';dest.mkdir(exist_ok=True)
    results={}
    for family in ('native','des'):
        for mode in ('immediate','tfs'):
            case=ROOT/'results'/f'seed416_fine_{family}_{mode}_24g_20261026'
            if family=='native':case=case/mode
            out=dest/f'{family}-{mode}';out.mkdir(exist_ok=True)
            results[family+'-'+mode]=(native if family=='native' else des)(case,out)
            print(family,mode,json.dumps(results[family+'-'+mode]['groups']['A']),flush=True)
    write(dest/'summary.json',results)
