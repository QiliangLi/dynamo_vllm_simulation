"""Adapt the existing AgentX358 runs for the fixed report renderer; no inference."""
import argparse, csv, hashlib, json, math, sys
from pathlib import Path
from collections import Counter, defaultdict
from statistics import mean
ROOT=Path.cwd(); AUDIT=Path('/home/zed/.local/share/state-aware-network/audits/server-dynamo-1.2')
parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,default=AUDIT/'agentx358_4qf0zin1');parser.add_argument('--stage',type=Path,default=ROOT/'temp/agentx358_report');parser.add_argument('--scaled-hit','--hit85',dest='scaled_hit',action='store_true');args=parser.parse_args()
run=args.run.resolve();stage=args.stage.resolve();stage.mkdir(exist_ok=True)
sys.path[:0]=[str(ROOT/'tooling'),str(ROOT/'server/auto_deploy_vllm/drt_dev/source_dev/tfs-runtime')]
from plotting.dataset import native_dataset,validate_dataset
from plotting.timeline import digest
read=lambda p:json.loads(p.read_bytes())
write=lambda p,v:p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
# Reuse the existing six-disk replay adapter, replacing only old workload sizes.
bw_source=(AUDIT/'abc_4w_8s_20261123/bandwidth.py').read_text()
bw_source=bw_source.replace('assert len(checked)==len(recorded)==3840','assert len(checked)==len(recorded)==len(history)')
bw_source=bw_source.replace('assert count==360 and len(loads)==240','assert count==len(report["requests"])')
bw_source=bw_source.replace("if kind=='layer_compute_start' and event['layer']==0: starts[rid]=event['occurred_mono']","if kind=='layer_compute_start' and event['layer']==0: starts.setdefault(rid,event['occurred_mono'])")
bw_source=bw_source.replace('assert rid not in times', 'pass # A request can span several prefill steps')
bw_source=bw_source.replace("times[rid]=event['timing']['duration_s']","times[rid]=times.get(rid,0.)+event['timing']['duration_s']")
(stage/'bandwidth.py').write_text(bw_source)
sys.path.insert(0,str(stage));from bandwidth import read_chart
trace={r['id']:r for r in map(json.loads,(run/'trace.jsonl').open())}
assert len(trace)==358
cases=[]
for mode,label,color in [('light','默认（全轻读）','#888888'),('tfs','TFS','#55A868')]:
 p=run/mode;view=stage/('view-'+mode);view.mkdir(exist_ok=True)
 st=read(p/'storage.final.json'); ids=read(p/'request_ids.json');st['hit_bindings']={v:v for v in ids.values()}
 write(view/'storage.projection.json',st)
 for f in [p/'config.json',p/'admission.final.json',p/'report.json',*p.glob('worker*.steps.jsonl')]:
  dest=view/('client_report.json' if f.name=='report.json' else f.name)
  if not dest.exists():dest.symlink_to(f)
 cases.append(dict(id=mode,label=label,color=color,path=str(view),run_metadata=dict(repeat=1,ordering='FCFS',alpha=.1)))
write(stage/'cases.json',dict(schema_version=1,title='AgentX 358 条｜四实例默认与 TFS 对照',cases=cases))
data=native_dataset(manifest=stage/'cases.json'); details=[];summary=[]
for case in data['cases']:
 mode=case['summary']['case'];p=run/mode;client=read(p/'report.json')['requests'];ledger=read(p/'admission.final.json');storage=read(p/'storage.final.json');gate=read(p/'read_admission.check.json');ck=read(p/'check.json')
 assert ck['status']==gate['status']=='pass' and len(client)==358
 reverse={v:k for k,v in read(p/'request_ids.json').items()}
 origin=ledger['origin_monotonic_s']+min(r['profile']['arrival_s'] for r in ledger['records'].values())
 ends={}; waits=defaultdict(Counter)
 for f in p.glob('worker*.steps.jsonl'):
  for e in map(json.loads,f.open()):
   if e.get('event')=='layer_compute_complete':ends.setdefault((e['req_ids'][0],e['layer']),e['occurred_mono']-storage['t0_monotonic_s'])
 for io in storage['layer_read_history']:
  eng=io['rid'];ly=io['layer'];key=reverse[eng[:36].replace('-','')];w=waits[key]
  w['bytes']+=io['bytes'];w['layers']+=1
  if ly:
   delay=max(0,io['completed_s']-ends[eng,ly-1]);w['later']+=delay;w['late']+=int(delay>0)
  else:w['first']+=io['completed_s']-io['submitted_s']
 for r in case['requests']:
  rid=r['id'];key=reverse[rid];row=trace[key];h=row['remote_prefix_tokens'];w=waits[key]
  assert w['bytes']==h*65536 and w['layers']==(16 if h else 0)
  r['cohort']='AgentX';pr=ledger['records'][rid]['profile']
  assert abs(pr['deadline_s']-pr['arrival_s']-4*pr['reference_service_s'])<1e-7
  details.append(dict(mode=mode,id=key,H=h,U=len(row['prompt_token_ids'])-h,worker=r['worker'],client_ttft_s=client[rid]['ttft_s'],online_heavy=int(ledger['storage_hints'][rid]['is_heavy_read']),first_wait_s=w['first'],later_wait_s=w['later'],read_bytes=w['bytes']))
 edges,demand,actual,check=read_chart(p,origin,case['summary']['span_s'])
 case['bandwidth']=dict(capacity_Bps=25e9,read_bytes=storage['bytes_transferred'],source='冻结六盘模型事件重放，逐层完成时刻核对；50 ms 分桶。需求为执行中请求的总读取字节/名义计算时间之和，非真实硬件采样。',intervals=[dict(start_s=float(a),end_s=float(b),actual_Bps=min(25e9,float(v)*1e9),demand_Bps=float(d)*1e9) for a,b,v,d in zip(edges,edges[1:],actual,demand)])
 case['bandwidth_unavailable']=None
 for f in [run/'trace.jsonl',p/'report.json',p/'request_ids.json',p/'read_admission.check.json',p/'check.json',p/'storage.final.json']:
  case['sources'][str(f)]=dict(sha256=digest(f),bytes=f.stat().st_size)
 times=sorted(r['ttft_s'] for r in client.values());caps=Counter(e['cap'] for e in map(json.loads,(p/'admission.events.jsonl').open()) if e.get('event')=='threshold_updated')
 summary.append(dict(mode=mode,finish_s=max(r['sent_perf_s']+r['e2e_s'] for r in client.values())-min(r['sent_perf_s'] for r in client.values()),mean_s=mean(times),p95_s=times[math.ceil(.95*len(times))-1],heavy=gate['heavy_hints'],grants=gate['grants'],skipped=gate['skipped_heavy_requests'],first_wait_s=sum(w['first'] for w in waits.values()),later_wait_s=sum(w['later'] for w in waits.values()),demand_peak_GBps=max(demand),caps=str(dict(caps)),replay_error_us=check['max_replay_error_us']))
# Check against the already published client metrics, not new simulated runs.
old=read(run/'comparison.json') if args.scaled_hit else read(ROOT/'artifacts/agentx_360_max5_over96000/comparison_358.json')
for s in summary:
 for key,prior in [('finish_s','finish'),('mean_s','ttft'),('p95_s','p95')]:assert abs(s[key]-old['runs'][s['mode']][prior])<1e-8
n_tokens=sum(len(r['prompt_token_ids']) for r in trace.values());h_tokens=sum(r['remote_prefix_tokens'] for r in trace.values());size=h_tokens*65536
s0,s1=summary
main='|策略|完成时间 s|平均 TTFT s|P95 s|重读请求|\n|---|---:|---:|---:|---:|\n'+''.join(f"|{s['mode']}|{s['finish_s']:.3f}|{s['mean_s']:.3f}|{s['p95_s']:.3f}|{s['heavy']}|\n" for s in summary)
wt='|策略|首层累计等待 s|后15层累计等待 s|额度满跳过请求|\n|---|---:|---:|---:|\n'+''.join(f"|{s['mode']}|{s['first_wait_s']:.3f}|{s['later_wait_s']:.3f}|{s['skipped']}|\n" for s in summary)
data['metadata']={'整理日期':'2026-11-24','负载':'连续窗口360条，删除窗口第8、71条（输入超过96000），保留358条及原12秒到达时刻；并非早期316/360条版本。','命中口径':f'沿用已构造trace的H/U，H按128块对齐；加权命中率{h_tokens/n_tokens:.4%}，不是本次按首次访问重新计算命中。','资源':'4实例，16层，max_num_seqs=1；6盘共用25 GB/s；FCFS，α=0.1；关闭强制准入。','重复':'同一trace，默认与TFS各一次；未新增实验。','时间口径':'第0章保留客户端发送至首token；标准主表及CDF使用central画像到首token，二者不混用。','SLO':'冻结profile.deadline_s，核对为arrival_s + 4 × reference_service_s；不是此次改成4倍全miss prefill。','输入SHA256':digest(run/'trace.jsonl'),'来源':str(run)}
data['execution_boundary']='仅处理已有CPU进程栈仿真日志，没有重跑仿真或真机实验。'
data['auxiliary_tables']={'client_summary':summary,'request_wait_details':details}
data['narrative']={'0':f"TFS 完成时间缩短 {(1-s1['finish_s']/s0['finish_s'])*100:.1f}%，平均 TTFT 增加 {(s1['mean_s']/s0['mean_s']-1)*100:.1f}%，P95 增加 {(s1['p95_s']/s0['p95_s']-1)*100:.1f}%。本轮改善整批完成时间，但没有改善平均首token延迟或尾延迟。\n\n以下表格沿用之前汇报的客户端口径。后续固定工具主表和CDF是central口径，其起点更晚。\n\n"+main,
'6':'两组实际模型读取量相同，TFS通过在线75分位分类和配额改变调度。默认强制全部轻读，仍执行全部KV读取。蓝色少或没有，表示没有重读标记，不表示没有读KV。\n\n'+wt+'\n首层等待为提交至读取完成；后续层等待为上一层算完至本层KV完成的正差。跨worker累计，不能直接从makespan减去。图2红色是IO活动区间减名义计算的并集，与此依赖等待口径不同。',
'7':'每策略只有一轮；时间线的计算是模型名义时间，不是NPU kernel实测。不能用这些图证明真机AIV/HBM竞争。客户端与central起点不同，尤其本轮存在进入画像前的延迟，不能混报。原始AgentX token内容在仿真输入中用占位token构造并隔离前缀；保留请求长度与命中量，不能据此验证真实语义或实际缓存持久化。',
'8':f'输入共 {n_tokens:,} token，命中 {h_tokens:,} token，读取 {size/1e9:.3f} GB。以25 GB/s满带宽传输至少需要 {size/25e9:.3f} 秒；12秒内发送的平均新增读取需求为 {size/12/1e9:.3f} GB/s。这是持续读取压力较高的工况，不是低平均负载下的短暂突发。\n\n默认/TFS 的额度更新分布分别为 {s0["caps"]} / {s1["caps"]}；TFS获批 {s1["grants"]} 次。',
'10':'若目标是整批完成，本轮TFS有收益；若目标是平均TTFT和P95，本轮没有收益。可从时间线检查限流后重读长尾，同时用累计等待表判断读取竞争是否减少。仅用本轮数据不能断言该收益在其他到达密度或真机上成立。'}
if args.scaled_hit:
 data['title']=f'AgentX 358 条，{h_tokens/n_tokens:.0%}命中｜四实例默认与 TFS 对照'
 data['metadata']['命中口径']=f'原H按同比例缩小，128-token块按余数分配；加权命中率{h_tokens/n_tokens:.4%}。总长度、逐token内容、请求顺序与到达时间均不变，减少的H转为U。不是每条请求独立设为同一命中率。'
 data['metadata']['重复']='本命中率版本默认和TFS各新增一轮；97%版本引用既有一轮。'
 data['execution_boundary']='本次新增两轮CPU进程栈仿真；97%版本仅引用历史数据；未执行真机实验。'
 prior=read(ROOT/'artifacts/agentx_360_max5_over96000/comparison_358.json')
 table='|命中率|策略|完成 s|平均TTFT s|P95 s|读取 GB|\n|---|---|---:|---:|---:|---:|\n'
 for rate,source in [('97.0063%',prior),(f'{h_tokens/n_tokens:.4%}',old)]:
  for mode,v in source['runs'].items():table+=f"|{rate}|{mode}|{v['finish']:.3f}|{v['ttft']:.3f}|{v['p95']:.3f}|{source['read_GB']:.3f}|\n"
 data['narrative']['8']+='\n\n'+table+f'\n降低命中率同时降低远端读量、增加实际计算量，不是只调带宽。原未命中392000 token，新未命中{n_tokens-h_tokens} token，约{(n_tokens-h_tokens)/392000:.2f}倍。两版本各一轮，不把波动视为确定的因果效果。'
if args.scaled_hit:
 changes='，'.join(f"{label}变化{(s1[key]/s0[key]-1)*100:+.1f}%" for key,label in [('finish_s','完成时间'),('mean_s','平均TTFT'),('p95_s','P95')])
 data['narrative']['0']='相对默认，TFS '+changes+'。每组只有一轮。以下为客户端口径；后续固定工具主表和CDF为central画像到首token口径。\n\n'+main
 data['narrative']['10']=f"本轮TFS重读{s1['heavy']}条，因额度满跳过{s1['skipped']}条。结合累计等待与执行时间线判断配额是否实际限制请求，不能只看重读标记数量。读取量下降伴随计算量增加，不能把全部变化归因于限流。"
validate_dataset(data);write(stage/'dataset.json',data);write(stage/'summary.json',summary)
print(json.dumps(summary,ensure_ascii=False,indent=2))
