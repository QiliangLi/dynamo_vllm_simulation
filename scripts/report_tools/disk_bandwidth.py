"""Replay recorded layer submissions on the frozen per-disk max-min model.

This is offline rate reconstruction, not another wall-clock performance run.
Every logical read completion and per-disk byte integral is checked against the
recorded execution. No average/full-bandwidth curve is invented from byte totals.
"""
from collections import defaultdict
import math
from pathlib import Path
import sys


def reconstruct(storage,cfg,formal,first,origin):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'dynamo_vllm_cpu_sim'))
    from sim.grouped_storage import GroupedStorage
    sc=cfg['storage'];assert sc['disks']>1 and sc.get('shared_link_Bps') is None
    assert sc['bandwidth_policy']=='max_min_fair' and sc.get('read_latency_s',0)==0
    assert cfg.get('wall_time_scale',1)==1
    bindings=storage['hit_bindings']
    def source(rid):return rid if rid in formal else bindings.get(rid.replace('-','').lower()[:32])
    history=[r for r in storage['layer_read_history'] if source(r['rid']) in formal]
    assert history
    events=sorted(enumerate(history),key=lambda pair:(pair[1]['submitted_s'],pair[0]))
    expected={};completed={};intervals=[];disk_integrals=[[] for _ in range(sc['disks'])]
    max_completion_error=0.;max_rate_roundoff=0.;per_disk_bytes=[0]*sc['disks']
    store=GroupedStorage(sc,cfg['block_size'])
    shift=storage['t0_monotonic_s']-origin-first

    def advance(target):
        nonlocal max_completion_error,max_rate_roundoff
        if target<=store.now:return
        rates=store._group_rates();actual=[0.]*sc['disks'];demands=[0.]*sc['disks']
        for path,rate in rates.items():
            read=store.queues[path][0];disk=path[0]
            actual[disk]+=rate*read.block_count;demands[disk]+=read.demand*read.block_count
        for d in range(sc['disks']):
            max_rate_roundoff=max(max_rate_roundoff,actual[d]-sc['disk_Bps'])
            assert actual[d]<=sc['disk_Bps']+max(.001,sc['disk_Bps']*1e-12)
            actual[d]=min(actual[d],sc['disk_Bps'])
            disk_integrals[d].append((target-store.now)*actual[d])
        if any(actual):
            intervals.append(dict(raw_start_s=store.now,raw_end_s=target,start_s=store.now+shift,
                end_s=target+shift,actual_Bps=math.fsum(actual),demand_Bps=math.fsum(demands),
                disk_actual_Bps=actual,disk_demand_Bps=demands))
        done=store.advance(target)
        for key in done:
            error=abs(target-expected[key]);max_completion_error=max(max_completion_error,error)
            assert error<=1e-7,('recorded completion differs from replay',key,error)
            completed[key]=target

    for index,row in events:
        target=row['submitted_s']
        while store.next_time()<=target:advance(store.next_time())
        advance(target)
        counts={int(d):n for d,n in storage['layer_loads'][row['rid']]['counts'].items()}
        assert row['disk_bytes']==[counts.get(d,0)*row['bytes']/sum(counts.values()) for d in range(sc['disks'])]
        key=(row['worker'],row['rid'],row['layer']);assert key not in expected
        expected[key]=row['completed_s']
        for d,value in enumerate(row['disk_bytes']):per_disk_bytes[d]+=value
        store.submit_counted(store.now,key,counts,row['bytes']/sum(counts.values()),
                             storage['layer_loads'][row['rid']]['demand'],row['read_latency_s'])
    while store.pending:advance(store.next_time())
    assert completed.keys()==expected.keys()
    replay_bytes=[math.fsum(v) for v in disk_integrals]
    errors=[]
    for d in range(sc['disks']):
        error=replay_bytes[d]-per_disk_bytes[d];errors.append(error)
        ulp_bound=math.fsum(e['disk_actual_Bps'][d]*sum(math.ulp(x) for x in
            (e['raw_start_s'],e['raw_end_s'],e['start_s'],e['end_s'])) for e in intervals)
        assert abs(error)<=max(1.,per_disk_bytes[d]*1e-11,ulp_bound),(d,error,ulp_bound)
    band=dict(capacity_Bps=sc['disks']*sc['disk_Bps'],read_bytes=sum(per_disk_bytes),intervals=intervals,
              timestamp_shift_s=shift,source='Recorded layer submissions replayed on frozen GroupedStorage; every completion and per-disk byte integral validated; independent disks, no borrowing')
    check=dict(status='passed',layer_reads=len(history),disks=sc['disks'],disk_Bps=sc['disk_Bps'],
        per_disk_bytes=per_disk_bytes,per_disk_integral_error_bytes=errors,
        max_completion_error_s=max_completion_error,max_rate_roundoff_Bps=max_rate_roundoff)
    return band,check


def disk_bins(band,span,case,width=.01):
    rows=[];n=len(band['intervals'][0]['disk_actual_Bps']);bins=math.ceil(span/width)
    actual=[[0.]*bins for _ in range(n)];demand=[[0.]*bins for _ in range(n)]
    for e in band['intervals']:
        for i in range(max(0,int(e['start_s']/width)),min(bins,math.ceil(e['end_s']/width))):
            dt=max(0.,min(e['end_s'],(i+1)*width)-max(e['start_s'],i*width))
            for d in range(n):
                actual[d][i]+=dt*e['disk_actual_Bps'][d];demand[d][i]+=dt*e['disk_demand_Bps'][d]
    cap=band['capacity_Bps']/n
    for d in range(n):
        for i in range(bins):
            rows.append(dict(case=case,disk=d,start_s=i*width,end_s=min(span,(i+1)*width),
                capacity_Bps=cap,actual_Bps=actual[d][i]/min(width,span-i*width),
                demand_Bps=demand[d][i]/min(width,span-i*width)))
    return rows
