"""Rebuild the Mooncake/block16 report from frozen exports, without simulation."""
from collections import Counter,defaultdict
import csv,hashlib,json,math,re,statistics,zipfile
from pathlib import Path
import numpy as np
import report_style as style
style.configure()
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.collections import PolyCollection

ROOT=Path(__file__).resolve().parent;D=ROOT/'report_data';FIG=ROOT/'figure';FIG.mkdir(exist_ok=True)
MODES=['fcfs','edf','old-tfs','new-tfs','idle-bypass']
LABEL=dict(zip(MODES,['直接放行 FCFS','直接放行 EDF','旧 TFS','新 TFS α=0.1','新 TFS＋强制准入']))
COL=dict(zip(MODES,['#888888','#C44E52','#DD8452','#55A868','#4C72B0']))
read=lambda p:json.loads(p.read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def csvout(name,values):
    with (D/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(values[0]));w.writeheader();w.writerows(values)
def percentile(values,q):return sorted(values)[max(0,math.ceil(len(values)*q)-1)]
def merge(items):
    out=[]
    for a,b in sorted(items):
        if b<=a:continue
        if out and a<=out[-1][1]+1e-12:out[-1][1]=max(out[-1][1],b)
        else:out.append([a,b])
    return out
def length(items):return math.fsum(b-a for a,b in merge(items))
def subtract(items,cuts):
    out=[];cuts=merge(cuts)
    for a,b in merge(items):
        cursor=a
        for l,r in cuts:
            if r<=cursor or l>=b:continue
            if l>cursor:out.append((cursor,min(l,b)))
            cursor=max(cursor,r)
            if cursor>=b:break
        if cursor<b:out.append((cursor,b))
    return out
def table(headers,records):
    return '|'+ '|'.join(headers)+'|\n|'+'|'.join(['---']+['---:']*(len(headers)-1))+'|\n'+''.join('|'+ '|'.join(str(x) for x in row)+'|\n' for row in records)
def image(name,caption):return f'\n![{caption}](figure/{name}.png)\n\n**读法**：{caption}\n\n'

meta=read(D/'input_metadata.json');source={r['id']:r for r in meta};inp=read(D/'input_check.json')
assert len(source)==360 and Counter(r['class_id'] for r in meta)==Counter(A=90,B=270)
N=360;W=12;B=90e9;L=78;expected=inp['total_read_bytes'];integral_tolerance=expected*1e-11;cases={};summaries=[];requests=[];devices=[];intervals=[];cdf=[];band_rows=[];checks={};deadline_by_id={}
for mode in MODES:
    raw=D/'raw'/mode;client=read(raw/'client_report.json');status=read(raw/'admission.final.json');cfg=read(raw/'config.json');storage=read(raw/'storage.projection.json')
    assert client['n_ok']==client['n_total']==N and not client['n_skipped'] and set(client['requests'])==set(source)
    assert len(status['records'])==432 and read(raw/'check.json')['requests']==432
    assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==cfg['admission']['reference_bandwidth_Bps']==B
    assert cfg['workers']==W and cfg['block_size']==16 and cfg['max_num_seqs']==4 and cfg['wall_time_scale']==1
    assert read(raw/'driver.json')['status']=='passed' and not read(raw/'driver.json')['guards'] and not read(raw/'driver.json')['alive']
    assert read(raw/'warmup.check.json')['coverage']==list(range(W))
    assert len(read(raw/'warmup.history.check.json')['excluded_history_ids'])==72
    assert not status['fatal'] and not status['invalid_reasons']
    assert status['execution_feedback']=='disabled' and status['delayed_events']==0
    assert not status['snapshot']['pending_ids'] and status['snapshot']['heavy_held']==0
    boundary=next(e['t'] for e in status['events'] if e['event']=='warmup_finished')
    thresholds=[e for e in status['events'] if e['event']=='threshold_updated' and e['t']>boundary]
    assert [e['history_size'] for e in thresholds]==list(range(1,N+1))
    rr={k:status['records'][k] for k in source};cp=client['requests']
    client_first=min(r['sent_perf_s'] for r in cp.values());client_end=max(r['sent_perf_s']+r['ttft_wall_s'] for r in cp.values())
    clock=read(raw/'clock.json');origin_gateway=clock['gateway_origin_monotonic_s'];assert clock['scale']==1
    first=origin_gateway+min(r['profile']['arrival_s'] for r in rr.values());end=origin_gateway+max(r['first_token_s'] for r in rr.values());span=end-first
    gc=read(raw/'gateway.gc.control.json');response_end=max(r['sent_perf_s']+r['e2e_wall_s'] for r in cp.values())
    assert gc['controls'][0]['t']<client_first and not gc['controls'][0]['after_enabled']
    assert gc['controls'][1]['t']>response_end and gc['controls'][1]['after_enabled']
    assert not [e for e in gc['history'] if client_first<=e['t']<=response_end]
    assert sum(r['submitted_read_bytes'] for r in rr.values())==expected
    samples={k:[] for k in ['all','A','B']};late=[];slo=defaultdict(list);deadlines=[];gw_wait=[];load_wait=[]
    for rid,r in rr.items():
        p=r['profile'];cls=source[rid]['class_id'];ttft=(r['first_token_s']-p['arrival_s'])*1000;limit=(p['deadline_s']-p['arrival_s'])*1000
        if rid in deadline_by_id:assert abs(deadline_by_id[rid]-limit)<1e-6
        else:deadline_by_id[rid]=limit
        assert r['state']=='FINISHED' and not r['lease_open'] and p['estimated_remote_tokens']==source[rid]['hit_tokens']
        assert r['submitted_read_bytes']==source[rid]['hit_tokens']*109824
        ok=r['first_token_s']<=p['deadline_s'];samples['all'].append(ttft);samples[cls].append(ttft);slo['all'].append(ok);slo[cls].append(ok);deadlines.append(limit)
        lateness=(cp[rid]['actual_arrival_s']-cp[rid]['planned_arrival_s'])*1000;late.append(lateness)
        wait=(r['submitted_s']-p['arrival_s'])*1000;gw_wait.append(wait);lw=(r['load_submit_s']-r['submitted_s'])*1000;load_wait.append(lw)
        requests.append(dict(mode=mode,id=rid,class_id=cls,worker=r['worker_id'],h_tokens=source[rid]['hit_tokens'],u_tokens=source[rid]['u_tokens'],
              gateway_ttft_ms=ttft,client_ttft_ms=cp[rid]['ttft_wall_s']*1000,slo_limit_ms=limit,slo_ok=int(ok),
              gateway_wait_ms=wait,submitted_to_read_ms=lw,arrival_lateness_ms=lateness,read_bytes=r['submitted_read_bytes']))
    bindings=storage['hit_bindings']
    def sid(raw):return bindings[raw.replace('-','')[:32]]
    comp=[];observed=defaultdict(list);io=[];batch=Counter();newtokens=Counter();model=Counter();sum_model=0.;steps=[]
    for file in sorted(raw.glob('worker*.steps.jsonl')):
        opened={}
        for line in file.open():
            e=json.loads(line)
            if e.get('num_scheduled_tokens'):
                ids={sid(k):v for k,v in e['num_scheduled_tokens'].items()}
                if set(ids)&set(source):
                    assert set(ids)<=set(source);newtokens.update(ids);batch[len(ids)]+=1
                    t=e['timing'];assert t['mode']=='glm5_d950pr' and t['real_prefill_tokens']==sum(ids.values())
                    assert t['batch_time_model']=='glm5_direct_sum_v1'
                    assert t['batch_composition']['launch_saving_fraction']==1.0
                    assert t['padded_prefill_tokens']==t['real_prefill_tokens']==sum(ids.values())
                    assert (t['reference_tp'],t['reference_dp'],t['reference_ep'])==(1,8,8)
                    sum_model+=t['duration_s'];steps.append(dict(worker=int(file.name.split('.')[0][6:]),step=e['step'],ids=list(ids),timing=t))
            if e.get('event')=='layer_compute_start':opened[e['step'],e['layer']]=e
            elif e.get('event')=='layer_compute_complete':
                start=opened[e['step'],e['layer']];ids=[sid(k) for k in start['req_ids']]
                if not set(ids)&set(source):continue
                assert set(ids)<=set(source);w=e['worker'];a=start['occurred_mono']-first;b=e['occurred_mono']-first;duration=start['duration_model_s']
                assert 0<=a<=b<=span+1e-6 and duration<=b-a+1e-6
                classes={source[k]['class_id'] for k in ids};model[w]+=duration;observed[w].append((a,b))
                comp.append(dict(worker=w,start_s=a,end_s=a+duration,state=next(iter(classes)) if len(classes)==1 else 'mixed',batch=len(ids)))
    assert set(newtokens)==set(source) and all(newtokens[k]==source[k]['u_tokens'] for k in source)
    assert sum(newtokens.values())==inp['new_tokens'] and len(comp)==sum(batch.values())*L
    assert sum(size*count for size,count in batch.items())>=N
    assert abs(sum(model.values())-sum_model)<1e-8 and set(model)==set(range(W))
    reads=[r for r in storage['layer_read_history'] if sid(r['rid']) in source]
    assert len(reads)==N*L and sum(r['bytes'] for r in reads)==expected
    origin=storage['t0_monotonic_s'];shift=origin-first;changes=defaultdict(list)
    demands={rid:load['demand']*sum(load['counts'].values()) for rid,load in storage['layer_loads'].items()}
    for i,r in enumerate(reads):
        a=r['submitted_s']+shift;b=r['completed_s']+shift;assert 0<=a<b<=span+1e-6
        io.append(dict(worker=r['worker'],start_s=a,end_s=b,state='io'))
        changes[r['submitted_s']].append((i,True,r['rid']));changes[r['completed_s']].append((i,False,r['rid']))
    active={};previous=None;band=[];integral=0.;oversub=0.;saturated=0.
    # Work-conserving single-disk max-min with zero startup: total actual rate
    # equals capacity exactly when at least one submitted layer is incomplete.
    for t,events in sorted(changes.items()):
        if previous is not None and t>previous:
            a=previous+shift;b=t+shift;actual=B if active else 0.;demand=sum(demands[rid] for rid in active.values());dt=t-previous
            integral+=dt*actual;oversub+=dt*(demand>B+.12);saturated+=dt*(actual>=.9*B)
            entry=dict(start_s=a,end_s=b,actual_Bps=actual,demand_Bps=demand,active_flows=len(active));band.append(entry)
            intervals.append(dict(mode=mode,start_ms=a*1000,end_ms=b*1000,actual_Bps=actual,demand_Bps=demand,active_flows=len(active)))
        for i,add,rid in events:
            if add:active[i]=rid
            else:del active[i]
        previous=t
    # Endpoints are rounded doubles. The final GroupedStorage advance clips
    # transferred bytes to each flow's remaining bytes, whereas the exported
    # occupancy union uses full capacity for that representable time slice.
    # Keep this few-byte error visible, without scaling rates or editing times.
    assert not active and abs(integral-expected)<integral_tolerance,(mode,integral-expected)
    for w in range(W):
        cc=[(r['start_s'],r['end_s']) for r in comp if r['worker']==w]
        assert abs(length(cc)-model[w])<1e-8
        red=length(subtract([(r['start_s'],r['end_s']) for r in io if r['worker']==w],cc));gray=span-model[w]-red
        assert gray>=-1e-7 and abs(model[w]+red+gray-span)<1e-8
        devices.append(dict(mode=mode,worker=w,model_compute_s=model[w],observed_compute_s=length(observed[w]),io_without_model_compute_s=red,
                            other_s=max(0,gray),completion_s=span,model_compute_pct=model[w]/span*100))
    ev=[e for e in status['events'] if e.get('id') in source]
    permits=[p for p in status['permits'].values() if p['id'] in source]
    forced=[p for p in permits if p.get('forced')]
    admission=dict(mode=mode,heavy_classified=sum(status['storage_hints'].get(k,{}).get('is_heavy_read',False) for k in source),
                   forced=len(forced),over_cap=sum(e.get('reason')=='idle_bypass' and e.get('over_cap',False) for e in ev),
                   denied=sum(e['event']=='read_permit_denied' for e in ev),
                   held_peak=max([e.get('held_after',0) for e in ev if e['event']=='admission_reserved'] or [0]))
    singleton_model_s=math.fsum(s['timing']['batch_composition']['sum_singleton_total_us'] for s in steps)*1e-6
    saved_launch_ms=math.fsum(s['timing']['batch_composition']['saved_launch_us_total'] for s in steps)/1000
    assert math.isclose(sum_model,singleton_model_s-saved_launch_ms/1000+math.fsum(s['timing']['engine_overhead_s'] for s in steps),abs_tol=1e-10)
    for step in steps:
        assert math.isclose(step['timing']['batch_composition']['saved_launch_us_total'],(len(step['ids'])-1)*3*7*78,abs_tol=1e-9)
    metrics=dict(mode=mode,completion_ms=span*1000,client_completion_ms=(client_end-client_first)*1000,requests_per_s=N/span,new_tokens_per_s=inp['new_tokens']/span,mean_batch_size=sum(k*n for k,n in batch.items())/sum(batch.values()),deadline_min_ms=min(deadlines),deadline_mean_ms=statistics.mean(deadlines),deadline_max_ms=max(deadlines),mean_ttft_ms=statistics.mean(samples['all']),p50_ms=percentile(samples['all'],.5),p95_ms=percentile(samples['all'],.95),
                 singleton_model_s=singleton_model_s,saved_launch_ms=saved_launch_ms,
                 slo_pct=statistics.mean(slo['all'])*100,mean_gateway_wait_ms=statistics.mean(gw_wait),mean_submitted_to_read_ms=statistics.mean(load_wait),
                 model_compute_s=sum_model,compute_pressure_pct=sum_model/W/4*100,model_compute_pct=sum_model/W/span*100,
                 request_step_memberships=sum(size*count for size,count in batch.items()),split_requests=sum(sum(rid in s['ids'] for s in steps)>1 for rid in source),steps=sum(batch.values()),multi_request_steps=sum(n for size,n in batch.items() if size>1),
                 lateness_mean_ms=statistics.mean(late),lateness_p95_ms=percentile(late,.95),lateness_max_ms=max(late),
                 mean_bandwidth_GBps=expected/span/1e9,demand_oversubscribed_pct=oversub/span*100,saturated_pct=saturated/span*100,
                 forced=admission['forced'],held_peak=admission['held_peak'])
    for cls in 'AB':metrics.update({cls+'_mean_ms':statistics.mean(samples[cls]),cls+'_slo_pct':statistics.mean(slo[cls])*100})
    for group,values in samples.items():
        for rank,t in enumerate(sorted(values),1):cdf.append(dict(mode=mode,group=group,rank=rank,n=len(values),ttft_ms=t,cdf=rank/len(values)))
    cases[mode]=dict(config=cfg,metrics=metrics,span=span,comp=comp,io=io,band=band,batch=dict(batch),samples=samples,late=late,deadlines=deadlines,admission=admission)
    checks[mode]=dict(status='passed',formal=N,warmup=72,layers_per_request=L,read_bytes=expected,new_tokens=sum(newtokens.values()),
                      bandwidth_integral_bytes=integral,bandwidth_error_bytes=integral-expected,time_accounts=W,
                      model_steps=sum(batch.values()),history_sizes='1..360',gc_events_in_formal=0,
                      formula_max_error_s=read(raw/'formula_check.json')['max_abs_error_s'])
    summaries.append(metrics)

provenance=read(D/'provenance.json');assert provenance['same_runtime_fingerprints'] and provenance['same_input']
for key in ['workers','scheduler','block_size','num_blocks','max_model_len','max_num_seqs','max_num_batched_tokens','enable_prefix_caching','compute','storage','request_profile','cache_model','kv_transfer_mode','layerwise','router_mode','wall_time_scale']:
    assert all(cases[m]['config'].get(key)==cases['fcfs']['config'].get(key) for m in MODES),key
assert {k for k in cases['fcfs']['config'] if cases['fcfs']['config'][k]!=cases['edf']['config'][k]}=={'policy','request_ordering'}
for mode in MODES:
    assert read(D/'raw'/mode/'routing.build.json')['native_sha256']==provenance['native_sha256']
    gc_end=read(D/'raw'/mode/'storage.gc.end.json')
    assert gc_end['policy']=='defer_during_run' and not gc_end['active'] and gc_end['enabled'] and not gc_end['failure']
    formal_begin=read(D/'raw'/mode/'storage.gc.formal.begin.json')
    assert formal_begin['active'] and not formal_begin['enabled']
    start=read(D/'raw'/mode/'clock.json')['gateway_origin_monotonic_s']+min(r['profile']['arrival_s'] for k,r in read(D/'raw'/mode/'admission.final.json')['records'].items() if k in source)
    finish=start+cases[mode]['span']
    assert formal_begin['events'][-1]['t']<start and gc_end['events'][-1]['t']>finish
    assert not [e for e in gc_end['events'] if start<=e['t']<=finish]
    checks[mode]['storage_gc_formal_events']=0
    checks[mode]['storage_gc_restored']=True
    if mode in ('edf','new-tfs','idle-bypass'): assert read(D/'raw'/mode/'edf_priority.check.json')['status']=='passed'
ca,cb=cases['new-tfs']['config'],cases['idle-bypass']['config'];aa,bb=dict(ca['admission']),dict(cb['admission'])
assert aa.pop('idle_bypass') is False and bb.pop('idle_bypass') is True and aa==bb
assert {k for k in ca if ca[k]!=cb[k]}=={'admission'}
csvout('run_summary.csv',summaries);csvout('requests.csv',requests);csvout('device_utilization.csv',devices);csvout('storage_intervals.csv',intervals);csvout('cdf.csv',cdf)
csvout('admission_summary.csv',[cases[m]['admission'] for m in MODES])
with (D/'prior100/run_summary.csv').open() as f:prior_summary={r['mode']:r for r in csv.DictReader(f)}
with (D/'prior100/requests.csv').open() as f:prior_requests={(r['mode'],r['id']):r for r in csv.DictReader(f)}
comparison=[]
for mode in MODES:
    old=prior_summary[mode];new=cases[mode]['metrics'];rr=[r for r in requests if r['mode']==mode]
    fixed_slo=sum(r['gateway_ttft_ms']<=float(prior_requests[mode,r['id']]['slo_limit_ms']) for r in rr)/N*100
    entry=dict(mode=mode,completion_100_ms=float(old['completion_ms']),completion_90_ms=new['completion_ms'],
        completion_change_pct=(new['completion_ms']/float(old['completion_ms'])-1)*100,
        mean_100_ms=float(old['mean_ttft_ms']),mean_90_ms=new['mean_ttft_ms'],
        mean_change_pct=(new['mean_ttft_ms']/float(old['mean_ttft_ms'])-1)*100,
        slo_100_pct=float(old['slo_pct']),slo_90_pct=new['slo_pct'],
        slo_change_pp=new['slo_pct']-float(old['slo_pct']),slo_90_on_100_deadlines_pct=fixed_slo)
    cases[mode]['comparison100']=entry;comparison.append(entry)
csvout('bandwidth_comparison.csv',comparison)
write(D/'summary.json',dict(runs=summaries,batch_histograms={m:cases[m]['batch'] for m in MODES},checks=checks))

fig,ax=plt.subplots(figsize=(13.5,6.8))
for i,m in enumerate(MODES):
    v=cases[m]['metrics'];ax.bar(i,v['slo_pct'],color=COL[m],width=.6);ax.text(i,v['slo_pct']+2,f"{v['slo_pct']:.1f}%",ha='center')

ax.set(xticks=range(len(MODES)),xticklabels=[LABEL[m]+f"\nA {cases[m]['metrics']['A_slo_pct']:.1f}% / B {cases[m]['metrics']['B_slo_pct']:.1f}%" for m in MODES],ylim=(0,105),ylabel='SLO 满足率（%）',title='图1｜Mooncake 360 · block16 · 五方案 SLO')
ax.grid(axis='y',alpha=.16);ax.set_axisbelow(True);fig.text(.5,.025,'每方案一次；A90/B270为固定展示分组，不等于在线重读分类；不含预热。',ha='center',fontsize=9);fig.tight_layout(rect=(0,.08,1,1));style.save(fig,FIG/'01_slo_bar.png')

horizon=math.ceil(max(c['span'] for c in cases.values())*1.03*10)/10
from refresh_timeline import refresh
refresh(ROOT)

u=np.array([[r['model_compute_pct'] for r in devices if r['mode']==m] for m in MODES])
fig,(ax,heat)=plt.subplots(1,2,figsize=(14,5.3),gridspec_kw={'width_ratios':[1,1.6]})
bp=ax.boxplot(u.T,patch_artist=True,showmeans=True,meanprops=dict(marker='D',markerfacecolor=style.UTIL_MEAN_COLOR,markeredgecolor=style.UTIL_MEAN_COLOR),medianprops=dict(color='black'))
for b in bp['boxes']:b.set(facecolor=style.UTIL_BOX_COLOR,alpha=.8)
ax.set(xticks=range(1,len(MODES)+1),xticklabels=['FCFS','EDF','旧TFS','新TFS\nα=0.1','新TFS\n＋强制'],ylim=(0,100),ylabel='模型计算时间 / 本方案完成窗口（%）',title='12个实例的模型计算占比');ax.grid(axis='y',alpha=.16)
im=heat.imshow(u,aspect='auto',**style.HEATMAP);heat.set(yticks=range(len(MODES)),yticklabels=[LABEL[m] for m in MODES],xticks=range(W),xlabel='原实例编号',title='计算占比热力图（0–100%）');fig.colorbar(im,ax=heat,label='%');fig.suptitle('图3｜不计IO、控制及休眠超时；不是硬件实测利用率');fig.tight_layout(rect=(0,0,1,.91));style.save(fig,FIG/'03_util_box_heatmap.png')

bin_s=.01;n=math.ceil(horizon/bin_s);series={};maximum=90.
for m in MODES:
    demand=np.zeros(n);actual=np.zeros(n)
    for e in cases[m]['band']:
        for i in range(max(0,int(e['start_s']/bin_s)),min(n,math.ceil(e['end_s']/bin_s))):
            overlap=max(0,min(e['end_s'],(i+1)*bin_s)-max(e['start_s'],i*bin_s));actual[i]+=overlap*e['actual_Bps'];demand[i]+=overlap*e['demand_Bps']
    assert abs(actual.sum()-expected)<integral_tolerance
    actual/=bin_s*1e9;demand/=bin_s*1e9;series[m]=(demand,actual);maximum=max(maximum,float(max(demand)))
    for i,(d,a) in enumerate(zip(demand,actual)):band_rows.append(dict(mode=m,bin_mid_ms=(i+.5)*10,bin_width_ms=10,demand_GBps=float(d),actual_GBps=float(a)))
csvout('bandwidth_10ms.csv',band_rows)
fig,axes=plt.subplots(len(MODES),1,figsize=(14.6,13.5),sharex=True,sharey=True);fig.subplots_adjust(left=.1,right=.985,bottom=.07,top=.91,hspace=.42)
for ax,m in zip(axes,MODES):
    d,a=series[m];x=(np.arange(n)+.5)*10;v=cases[m]['metrics']
    ax.plot(x,np.maximum(d,.5),color=style.BANDWIDTH_COLOR['demand'],lw=.9,label='需求 Σ申请率');ax.plot(x,np.maximum(a,.5),color=style.BANDWIDTH_COLOR['actual'],lw=1,label='实际 Σ分配率')
    ax.axhline(90,color='black',ls='--',lw=1);ax.set(yscale='log',ylim=(.5,maximum*1.4),ylabel=LABEL[m]+'\nGB/s',xlim=(0,horizon*1000));ax.grid(which='both',alpha=.16)
    ax.set_title(f"需求超订 {v['demand_oversubscribed_pct']:.1f}%｜实际饱和 {v['saturated_pct']:.1f}%｜平均 {v['mean_bandwidth_GBps']:.1f} GB/s",loc='right',fontsize=9)
axes[0].legend(loc='lower right');axes[-1].set_xlabel('ms；10ms精确区间积分；对数底线0.5 GB/s仅用于显示零值');fig.suptitle('图4｜90 GB/s共享池 · 逐请求逐层读区间重建 · 仅正式请求',y=.985);style.save(fig,FIG/'04_bandwidth.png')

def ecdf(ax,values,color,label,ls='-'):
    x=np.array(sorted(values));y=np.arange(1,len(values)+1)/len(values);assert y[-1]==1 and np.all(np.diff(x)>=0)
    ax.step(np.r_[0,x],np.r_[0,y],where='post',color=color,lw=1.5,ls=ls,label=label)
fig,ax=plt.subplots(figsize=(10.2,6))
for m in MODES:
    v=cases[m]['samples']['all'];ecdf(ax,v,COL[m],f'{LABEL[m]}｜p50 {percentile(v,.5):.0f} / p95 {percentile(v,.95):.0f} ms')
ecdf(ax,cases['fcfs']['deadlines'],'#444444','逐请求期限宽度分布',':')
ax.set(xlim=(0,None),ylim=(0,1.02),xlabel='网关 TTFT（ms，包含中央准入等待）',ylabel='累计分布 CDF',title='图5｜全部360条 TTFT CDF · K=1 · block16');ax.grid(alpha=.18);ax.legend(loc='lower right');fig.tight_layout();style.save(fig,FIG/'05_cdf.png')
fig,axes=plt.subplots(1,2,figsize=(12,4.7),sharey=True)
for ax,cls in zip(axes,'AB'):
    for m in MODES:ecdf(ax,cases[m]['samples'][cls],COL[m],LABEL[m])
    ax.set(xlim=(0,None),ylim=(0,1.02),xlabel='网关 TTFT（ms）',title=f'{cls}：每方案{sum(r["class_id"]==cls for r in meta)}条');ax.grid(alpha=.18);ax.legend(loc='lower right',fontsize=8)
axes[0].set_ylabel('累计分布 CDF');fig.suptitle('图6｜固定展示分类；期限逐请求不同，不画统一期限线');fig.tight_layout(rect=(0,0,1,.9));style.save(fig,FIG/'06_cdf_by_class.png')
fig,ax=plt.subplots(figsize=(10.2,5))
for m in MODES:ecdf(ax,cases[m]['late'],COL[m],f"{LABEL[m]}｜p95 {percentile(cases[m]['late'],.95):.0f} ms")
ax.set(xlim=(0,None),ylim=(0,1.02),xlabel='实际客户端发送−计划发送（ms）',ylabel='累计分布 CDF',title='图7｜实际投递迟到：保留壁钟调度与宿主开销');ax.grid(alpha=.18);ax.legend(loc='lower right');fig.tight_layout();style.save(fig,FIG/'07_arrival_lateness.png')



from report_text import render
name='实验结果-Mooncake360-90GB直接组合五方案-20261029.md'
(ROOT/name).write_text(render(cases,inp,provenance,checks,table,image,LABEL,MODES,read,D),encoding='utf-8')
from verify_report import verify
print(json.dumps(verify(ROOT),ensure_ascii=False),flush=True)
