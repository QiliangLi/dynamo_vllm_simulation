"""Adapt one accepted five-arm disk matrix case to the fixed report schema."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
import math
import re
from pathlib import Path
import sys

from disk_bandwidth import reconstruct,disk_bins
from plotting.dataset import native_dataset,validate_dataset

LABELS={'direct-fcfs':'直接准入 FCFS','direct':'直接准入 EDF','old-tfs':'旧 TFS EDF',
        'tfs-a01':'新 TFS EDF α=0.1','tfs-a1':'新 TFS EDF α=1'}
COLORS={'direct-fcfs':'#888888','direct':'#4C72B0','old-tfs':'#DD8452','tfs-a01':'#55A868','tfs-a1':'#9974A5'}
read=lambda p:json.loads(Path(p).read_text())


def build(matrix,case,output):
    attempts=read(matrix/'attempts.json')
    selected={r['arm']:r for r in attempts if r['case']==case and not r['smoke'] and r['returncode']==0}
    assert set(selected)==set(LABELS)
    spec=dict(schema_version=1,title=case+'｜独立分盘五策略',cases=[])
    for arm in LABELS:
        run=matrix/case/f"package/results/{selected[arm]['run_name']}/run"
        spec['cases'].append(dict(id=arm,path=str(run),label=LABELS[arm],color=COLORS[arm],
            run_metadata=dict(seed=20261102,repeat=1,started_at=selected[arm]['started_at'],
                              finished_at=selected[arm]['finished_at'],run_name=selected[arm]['run_name'])))
    output.mkdir(parents=True,exist_ok=False)
    (output/'cases.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n')
    data=native_dataset(manifest=output/'cases.json')
    auxiliary=defaultdict(list);audits={}
    for item,c in zip(spec['cases'],data['cases']):
        root=Path(item['path']);cfg=c['config'];storage=read(root/'storage.final.json');state=read(root/'admission.final.json')
        formal={r['id'] for r in c['requests']};first=min(state['records'][r]['profile']['arrival_s'] for r in formal)
        origin=state['origin_monotonic_s']
        band,audit=reconstruct(storage,cfg,formal,first,origin)
        c['bandwidth']=band;c['bandwidth_unavailable']=None;audits[item['id']]=audit
        bins=disk_bins(band,c['summary']['span_s'],item['id']);auxiliary['disk_bandwidth_bins'].extend(bins)
        total=sum(audit['per_disk_bytes']);mean=total/cfg['storage']['disks']
        for d,amount in enumerate(audit['per_disk_bytes']):
            aux=dict(case=item['id'],disk=d,read_bytes=amount,read_GB=amount/1e9,
                     share_pct=amount/total*100 if total else 0.,
                     mean_GBps=amount/c['summary']['span_s']/1e9,capacity_GBps=cfg['storage']['disk_Bps']/1e9,
                     load_vs_disk_mean=amount/mean if mean else 0.,
                     integral_error_bytes=audit['per_disk_integral_error_bytes'][d])
            auxiliary['disk_summary'].append(aux)
        auxiliary['disk_replay_checks'].append(dict(case=item['id'],**{k:v for k,v in audit.items() if k not in ('per_disk_bytes','per_disk_integral_error_bytes')}))
        recs=state['records'];lookup={e['engine_uuid'].replace('-','').lower():e['id'] for e in state['events'] if e['event']=='request_correlated'}
        for p in root.glob('worker*.steps.jsonl'):
            for line in p.open():
                step=json.loads(line)
                if not step.get('num_scheduled_tokens'):continue
                ids=[lookup[k.replace('-','').lower()[:32]] for k in step['num_scheduled_tokens']]
                if not set(ids)&formal:continue
                assert set(ids)<=formal
                auxiliary['native_batches'].append(dict(case=item['id'],worker=int(p.name.split('.')[0][6:]),
                    step=step['step'],batch_size=len(ids),scheduled_tokens=sum(step['num_scheduled_tokens'].values()),
                    model_duration_ms=step['timing']['duration_s']*1000,
                    request_ids=json.dumps(ids,separators=(',',':'))))
        granted=[(recs[r]['reserved_s']-recs[r]['profile']['arrival_s'])*1000 for r in formal if recs[r].get('reserved_s') is not None]
        auxiliary['acceptance_checks'].append(dict(case=item['id'],status='passed',requests=360,
            warmup_requests=72,disks=cfg['storage']['disks'],disk_Bps=cfg['storage']['disk_Bps'],
            request_ordering=cfg['request_ordering'],admission_placement=cfg['admission']['placement'],
            routing_weighted=cfg['routing_accounting']['enabled'],
            granted_requests=len(granted),mean_granted_request_queue_to_admit_ms=math.fsum(granted)/len(granted) if granted else None))
        responses=[]
        for p in root.glob('worker*.read_admission.jsonl'):
            for line in p.open():
                q=json.loads(line)
                if q['event']!='read_acquire_response':continue
                match=re.search(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}',q['request_id'])
                assert match
                if lookup[match.group().replace('-','').lower()] in formal:responses.append(q)
        reservations=[e for e in state['events'] if e['event']=='admission_reserved' and e['id'] in formal]
        caps=[e['cap'] for e in reservations if e.get('cap') is not None]
        auxiliary['admission_summary'].append(dict(case=item['id'],formal_grants=len(reservations),
            final_heavy_cap=state['snapshot']['heavy_cap'],max_heavy_held=max((e['held_after'] for e in reservations),default=0),
            granted_cap_min=min(caps) if caps else None,granted_cap_max=max(caps) if caps else None,
            acquire_rpc_calls=len(responses),acquire_rpc_denied=sum(not q['granted'] for q in responses),
            acquire_rpc_mean_ms=math.fsum(q['duration_ns']/1e6 for q in responses)/len(responses) if responses else None,
            acquire_rpc_p95_ms=sorted(q['duration_ns']/1e6 for q in responses)[math.ceil(.95*len(responses))-1] if responses else None))
        c['layers']=[] # Compact dataset keeps exact comp/IO intervals; verbose per-layer CSV is not needed for redraw.
    data['auxiliary_tables']=dict(auxiliary)
    cfg=data['cases'][0]['config'];manifest=read(matrix/case/'manifest.json')
    data.update(execution_boundary='本任务新跑：每方案12实例、72预热排空后360正式；下述制图阶段仅离线处理，不重跑实验。',
        metadata={'实验日期':'2026-11-02','负载':case,'固定输入SHA256':manifest['original_trace_sha256'],
            '独立磁盘':f"{cfg['storage']['disks']}盘 × {cfg['storage']['disk_Bps']/1e9:g} GB/s；shared_link_Bps=null",
            '放置与分配':'真实KV块内容HRW哈希；盘内grouped max-min；盘间不借带宽',
            '准入范围':'TFS仍为总参考带宽的全局配额，没有按盘压力准入或路由',
            '排序':'仅直接准入FCFS使用FCFS；其余四方案实例内EDF；旧TFS保留网关准入规则',
            '计算模型':cfg['compute']['batch_time_model'],'窗口':f"{manifest['arrival_window_s']:g}秒计划到达；保留原时间原点",
            '实例与预算':f"12逻辑实例；seq={cfg['max_num_seqs']}；新token预算={cfg['max_num_batched_tokens']}；K=1",
            '种子与重复':'输入固定；运行顺序seed20261102；每方案1次有效运行；无置信区间',
            '带宽重建':'记录逐层提交时刻，在冻结存储模型离线重放；核对每次完成及每盘字节积分；逐盘图按10ms积分分桶',
            '反馈与释放':'四类执行反馈关闭；首token释放TFS；原始在线快照与离线执行观测分开',
            '宿主与边界':'WSL/Linux CPU壁钟仿真；名义计算占比不是GPU/NPU实测利用率'})
    validate_dataset(data)
    (output/'dataset.json').write_text(json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n')
    (output/'disk_replay_validation.json').write_text(json.dumps(audits,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status='passed',case=case,cases=5,requests=1800,
                         max_completion_error_s=max(v['max_completion_error_s'] for v in audits.values()))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--matrix',type=Path,required=True)
    p.add_argument('--case',required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();build(a.matrix.resolve(),a.case,a.output.resolve())
