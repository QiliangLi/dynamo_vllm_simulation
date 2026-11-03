"""Portable K=1 benchmark runner: frozen inputs, 72 warmups, native stack and audits."""
import argparse
from collections import Counter
from copy import deepcopy
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
REPO=ROOT.parent
ARMS={'direct-fcfs':1.,'direct':1.,'old-tfs':1.,'tfs-a01':.1,'tfs-a1':1.}
DEFAULT_ARMS=('direct','tfs-a01','tfs-a1')
read=lambda p:json.loads(Path(p).read_text())


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


MANAGER=load_module('benchmark_manage',REPO/'benchmark/manage.py')
CASES=MANAGER.case_names(supported_only=True)


def effective_config(cfg,manifest,contract=None):
    """Explicit AgentX formula guard; exported/historical configs stay frozen."""
    cfg=deepcopy(cfg)
    if manifest.get('runner_profile')=='agentx360_v1':
        assert contract and contract['long_context_formula_extrapolation'] is True
        overrides=contract['runtime_overrides']
        assert overrides=={'compute':{'context_length_limit':300481}}
        cfg['compute'].update(overrides['compute'])
    return cfg


def apply_storage_layout(cfg,layout=None):
    cfg=deepcopy(cfg)
    if layout is not None:
        assert set(layout)=={'disks','disk_Bps'} and type(layout['disks']) is int and layout['disks']>1
        assert type(layout['disk_Bps']) in (int,float) and math.isfinite(layout['disk_Bps']) and layout['disk_Bps']>0
        total=cfg['admission']['reference_bandwidth_Bps']
        assert layout['disks']*layout['disk_Bps']==total, 'layout must retain total reference bandwidth'
        cfg['storage'].update(**layout,shared_link_Bps=None)
    return cfg


def arm_config(cfg,arm,manifest):
    cfg=deepcopy(cfg);new=arm in ('tfs-a01','tfs-a1')
    cfg['admission'].update(mode='tfs' if new or arm=='old-tfs' else 'immediate',
        placement='scheduler' if new else 'gateway',classification='arrival' if new else 'dynamic',
        idle_bypass=False,aging_enabled=False)
    cfg.update(policy='fcfs' if arm=='direct-fcfs' else 'priority',
               request_ordering='fcfs' if arm=='direct-fcfs' else 'edf')
    cfg['routing_accounting'].update(enabled=new)
    if new or manifest.get('runner_profile') not in ('agentx360_v1','synthetic_abc360_v1'):
        cfg['routing_accounting']['waiting_factor']=ARMS[arm]
    return cfg


def audit_synthetic_plan(profiles,manifest,contract):
    rows=sorted(profiles.values(),key=lambda p:p['arrival_s'])
    assert len(rows)==360 and Counter(p['class_id'] for p in rows)=={'A':120,'B':120,'C':120}
    assert rows[0]['arrival_s']==contract['first_planned_arrival_s']>0
    assert rows[-1]['arrival_s']==contract['last_planned_arrival_s']==6
    for p in rows:
        shape=manifest['class_shapes'][p['class_id']]
        assert (p['h'],p['u'],p['n'])==(shape['hit_tokens'],shape['new_tokens'],sum(shape.values()))
    assert math.isclose(math.fsum(p['compute_s'] for p in rows),manifest['timing']['reference_compute_instance_s'],rel_tol=1e-12)


def apply_storage_backend(cfg, backend="python"):
    cfg=deepcopy(cfg)
    if backend not in ("python","cpp"):raise ValueError("unknown storage backend")
    if backend=="cpp":
        from sim.cpp_grouped_storage import storage_class
        sc=cfg["storage"]
        if sc.get("shared_link_Bps") is not None:raise ValueError("cpp requires explicit independent disks (--disks/--disk-gbps)")
        cfg["storage"]["backend"]="cpp"
        grouped=sc.get("bandwidth_policy")=="max_min_fair" and sc.get("max_min_read_representation","grouped")=="grouped"
        storage_class(cfg,grouped,sc.get("bandwidth_policy")=="sa_max_min")
    return cfg


def prepare(case,folder,overlay,layout=None,backend="python"):
    package=folder/'package';package.mkdir(parents=True)
    for name in ('sim','scripts','configs','patched'):
        shutil.copytree(ROOT/name,package/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('versions.json','source-fingerprints.json'):shutil.copy2(ROOT/name,package/name)
    for name in ('.venv','upstream'):(package/name).symlink_to((ROOT/name).resolve(),target_is_directory=True)
    inp=package/'results/input360'
    MANAGER.export(case,inp,quiet=True)
    manifest=read(inp/'manifest.json');assert sha(inp/'trace.jsonl')==manifest['original_trace_sha256']
    contract=read(inp/'run_contract.json') if (inp/'run_contract.json').exists() else None
    cfg=apply_storage_backend(apply_storage_layout(effective_config(read(inp/'config.json'),manifest,contract),layout),backend)
    cfg['routing_accounting']['python_path']=str(overlay)
    assert cfg['wall_time_scale']==1 and cfg['workers']==12
    bandwidth=manifest['shared_storage_Bps']
    assert cfg['storage']['disks']*cfg['storage']['disk_Bps']==bandwidth
    assert cfg['storage']['shared_link_Bps']==(None if layout else bandwidth)
    assert cfg['admission']['reference_bandwidth_Bps']==bandwidth
    write(inp/'config.json',cfg)
    write(folder/'experiment.json',dict(storage_layout=layout,storage_backend=backend,audit_queue_policy=True,
          admission_scope='global scalar reference bandwidth; no per-disk admission or routing',
          original_benchmark_input_sha256=manifest['original_trace_sha256']))
    sys.path.insert(0,str(package))
    from sim.request_profile import normalize_config,build_profile
    cfg=normalize_config(cfg)
    profiles=[]
    with (inp/'trace.jsonl').open() as f:
        for line in f:
            r=json.loads(line);p=build_profile(r,cfg)
            profiles.append(dict(id=r['id'],class_id=r['class_id'],n=len(r['prompt_token_ids']),
                h=r['hit_tokens'],u=r['u_tokens'],arrival_s=r['arrival_s'],read_bytes=p.estimated_read_bytes,
                compute_s=p.standalone_compute_s,T0_s=p.reference_service_s,
                deadline_width_s=p.deadline_s-p.arrival_s))
    assert len(profiles)==360 and sum(r['u'] for r in profiles)==manifest['new_tokens']
    assert sum(r['read_bytes'] for r in profiles)==manifest['read_bytes']
    write(folder/'profiles.json',profiles);write(folder/'manifest.json',manifest)
    fingerprints={str(p.relative_to(package)):sha(p) for name in ('sim','scripts','configs','patched')
                  for p in (package/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    write(folder/'frozen.json',dict(files=fingerprints,input_sha256=sha(inp/'trace.jsonl'),
        config_sha256=sha(inp/'config.json'),native_sha256=sha(overlay/'dynamo/_core.abi3.so'),K=1,
        native_build_sha256=sha(overlay.parent/'build.json'),experiment_sha256=sha(folder/'experiment.json')))


def audit(folder,arm,smoke,run_name=None):
    run=folder/f'package/results/{run_name or arm}/run';cfg=read(run/'config.json');state=read(run/'admission.final.json')
    variant=read(folder/'experiment.json') if (folder/'experiment.json').exists() else {}
    layout=variant.get('storage_layout')
    backend=variant.get('storage_backend','python')
    assert cfg['storage'].get('backend','python')==backend
    storage_snapshot=read(run/'storage.final.json')
    assert storage_snapshot.get('storage_backend','python')==backend
    if backend=='cpp':
        assert storage_snapshot['native_build']['source_sha256']==sha(folder/'package/sim/native/grouped_storage.cpp')
    driver=read(run.parent/'driver.json');client=read(run/'measured/report.json')
    profiles={r['id']:r for r in read(folder/'profiles.json')};manifest=read(folder/'manifest.json')
    records={k:v for k,v in state['records'].items() if k in profiles}
    assert driver['status']=='passed' and not driver['guards'] and not driver['alive']
    assert len(records)==client['n_total']==client['n_ok'] and not client['n_skipped']
    if 'requests' in client:
        assert set(records)==set(client['requests']), 'formal IDs differ from HTTP client'
    assert not state['fatal'] and not state['invalid_reasons'] and not state.get('pending_engine_events')
    if state.get('execution_feedback')=='disabled':
        raw=read(run/'admission.runtime.final.json')
        assert state['load_observation_source']=='offline_worker_logs'
        assert all(r['load_attempts']==0 and r['submitted_read_bytes']==0 for r in raw['records'].values())
        assert not any(e['event']=='feedback_received' and e.get('engine_id') for e in raw['events'])
        assert read(run/'admission_rpc.check.json')['status']=='passed'
    assert not state['snapshot']['pending_ids'] and not state['snapshot']['heavy_held']
    ttft=[];slo=0
    for rid,r in records.items():
        p=profiles[rid];a=r['profile']
        assert r['state']=='FINISHED' and not r['lease_open']
        assert r['submitted_read_bytes']==p['read_bytes']
        assert a['prompt_tokens']==p['n'] and a['estimated_remote_tokens']==p['h']
        assert abs((a['deadline_s']-a['arrival_s'])-p['deadline_width_s'])<1e-8
        ttft.append((r['first_token_s']-a['arrival_s'])*1000)
        slo+=r['first_token_s']<=a['deadline_s']
    if not smoke:
        assert len(records)==360
        assert sum(profiles[r]['u'] for r in records)==manifest['new_tokens']
        assert sum(r['submitted_read_bytes'] for r in records.values())==manifest['read_bytes']
        warm=read(run/'warmup.check.json')
        assert warm['requests']==72 and warm['coverage']==list(range(12))
    bandwidth=manifest['shared_storage_Bps']
    assert cfg['wall_time_scale']==1
    if layout:
        assert cfg['storage']['disks']==layout['disks'] and cfg['storage']['disk_Bps']==layout['disk_Bps']
        assert cfg['storage']['disks']*cfg['storage']['disk_Bps']==bandwidth and cfg['storage']['shared_link_Bps'] is None
    else:assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==bandwidth
    assert cfg['admission']['reference_bandwidth_Bps']==bandwidth
    assert cfg['request_ordering']==('fcfs' if arm=='direct-fcfs' else 'edf')
    assert cfg['admission']['idle_bypass'] is False
    if arm in ('tfs-a01','tfs-a1'):
        assert cfg['admission']['placement']=='scheduler' and cfg['admission']['classification']=='arrival'
        assert cfg['routing_accounting']['waiting_factor']==ARMS[arm]
    if arm=='old-tfs':assert cfg['admission']['placement']=='gateway' and cfg['admission']['classification']=='dynamic' and not cfg['routing_accounting']['enabled']
    if variant.get('audit_queue_policy'):
        from sim.request_profile import normalize_config
        base=read(folder/'package/results/input360/config.json')
        assert cfg==normalize_config({**arm_config(base,arm,manifest),**({'workers':2} if smoke else {})})
        policies={}
        for p in (run/'fine_trace').glob('trace-*.jsonl'):
            ready=read(p.parent/p.name.replace('trace-','ready-').replace('.jsonl','.json'))
            for line in p.open():
                event=json.loads(line)
                if event['event']=='native_queue_policy':
                    policies[int(ready['worker'])]=event
        expected_policy='fcfs' if arm=='direct-fcfs' else 'priority'
        expected_queue='FCFSRequestQueue' if arm=='direct-fcfs' else 'PriorityRequestQueue'
        assert set(policies)==set(range(cfg['workers']))
        assert all(p['policy']==expected_policy and p['queue_type']==expected_queue for p in policies.values()),policies
        write(run/'native_queue_policy.check.json',dict(status='passed',policy=expected_policy,queue_type=expected_queue,workers=sorted(policies)))
    if manifest.get('runner_profile') in ('agentx360_v1','synthetic_abc360_v1'):
        from sim.request_profile import normalize_config
        contract=read(folder/'package/results/input360/run_contract.json')
        expected=apply_storage_layout(arm_config(effective_config(contract['arms']['direct'],manifest,contract),arm,manifest),layout)
        expected=apply_storage_backend(expected,backend)
        expected['routing_accounting']['python_path']=cfg['routing_accounting']['python_path']
        if smoke:expected['workers']=2
        assert cfg==normalize_config(expected), 'effective config differs from frozen arm contract'
        assert state['execution_feedback']=='disabled'
    if manifest.get('runner_profile')=='agentx360_v1':
        assert cfg['compute']['context_length_limit']==300481
        assert state['execution_feedback']=='disabled'
        assert contract['startup']=={'admission_ready_timeout_wall_s':180}
        assert cfg['admission']['rpc_timeout_wall_s']==5.0
        assert client['requests'] and all(k.startswith('agentx:') for k in client['requests'])
        assert max(profiles[r]['n'] for r in records)==300480
        assert len(records)==(6 if smoke else 360)
        steps=[json.loads(line) for p in run.glob('worker*.steps.jsonl') for line in p.read_text().splitlines() if line.strip()]
        long_steps=[r['timing'] for r in steps if (r.get('timing') or {}).get('max_input_length',0)>131072]
        assert long_steps and all(t['formula_context_extrapolation'] and t['context_length_limit']==300481
                                  for t in long_steps), 'long-context execution was not observed'
    if manifest.get('runner_profile')=='synthetic_abc360_v1':
        audit_synthetic_plan(profiles,manifest,contract)
        assert len(records)==(6 if smoke else 360)
        assert Counter(profiles[r]['class_id'] for r in records)==dict.fromkeys(('A','B','C'),2 if smoke else 120)
        assert 'context_length_limit' not in cfg['compute']
        if not smoke:
            assert state['snapshot']['heavy_cap']==contract['expected_stable_heavy_cap']==3
            assert math.isclose(state['snapshot']['heavy_threshold_Bps']/1e9,contract['expected_stable_threshold_GBps'],rel_tol=1e-12)
            planned=[json.loads(line) for line in (run.parent/'measured_trace.jsonl').read_text().splitlines()]
            assert [r['id'] for r in planned]==list(profiles), 'request order changed'
            assert all(r['arrival_s']==profiles[r['id']]['arrival_s'] for r in planned), 'arrival plan changed'
        if cfg['request_ordering']=='edf' and not (run/'edf_priority.check.json').exists():
            checker=load_module('synthetic_edf_check',folder/'package/scripts/check_edf_priority.py')
            write(run/'edf_priority.check.json',checker.check(run))
    ts=sorted(ttft);n=len(ts)
    result=dict(status='passed',case=manifest['case'],arm=arm,smoke=smoke,requests=n,
        warmup_requests=0 if smoke else 72,K=1,shared_storage_Bps=bandwidth,
        mean_ttft_ms=sum(ts)/n,p95_ttft_ms=ts[math.ceil(n*.95)-1],slo_pct=slo/n*100,
        completion_ms=(max(r['first_token_s'] for r in records.values())-min(r['profile']['arrival_s'] for r in records.values()))*1000,
        measured_read_bytes=sum(r['submitted_read_bytes'] for r in records.values()))
    if manifest.get('runner_profile')=='agentx360_v1':
        assert max(t['max_input_length'] for t in long_steps)==300480
        result.update(formula_context_limit=300481,long_context_steps_including_warmup=len(long_steps),
                      maximum_executed_context=300480,
                      long_context_requests=sum(profiles[r]['n']>131072 for r in records),
                      context_extrapolation=True,execution_feedback='disabled')
    if manifest.get('runner_profile')=='synthetic_abc360_v1':
        result.update(execution_feedback='disabled',context_extrapolation=False,
            class_counts=dict(Counter(profiles[r]['class_id'] for r in records)),
            planned_first_arrival_s=min(p['arrival_s'] for p in profiles.values()),planned_last_arrival_s=6,
            final_heavy_cap=state['snapshot']['heavy_cap'],final_heavy_threshold_Bps=state['snapshot']['heavy_threshold_Bps'])
    if layout:
        storage=read(run/'storage.final.json')
        result.update(storage_disks=layout['disks'],disk_Bps=layout['disk_Bps'],shared_link_Bps=None,
                      disk_bytes_including_warmup=storage['disk_bytes_submitted'],disk_peak_Bps=storage['disk_peak_Bps'])
    result['storage_backend']=backend
    write(run/'benchmark.check.json',result)
    return result


def execute(folder,arm,overlay,smoke,run_name=None):
    frozen=read(folder/'frozen.json');package=folder/'package'
    assert all(sha(package/name)==expected for name,expected in frozen['files'].items())
    assert sha(package/'results/input360/trace.jsonl')==frozen['input_sha256']
    assert sha(package/'results/input360/config.json')==frozen['config_sha256']
    assert sha(overlay/'dynamo/_core.abi3.so')==frozen['native_sha256']
    assert sha(overlay.parent/'build.json')==frozen['native_build_sha256']
    if 'experiment_sha256' in frozen:assert sha(folder/'experiment.json')==frozen['experiment_sha256']
    sys.path[:0]=[str(package),str(package/'scripts')]
    runtime=load_module('benchmark_runtime',package/'scripts/benchmark_runtime.py')
    runtime.A=folder;runtime.ROOT=package;runtime.OVERLAY=overlay
    runtime.CASE_NAME=run_name or arm;runtime.ALPHA=ARMS[arm]
    runtime.ARM_CONFIG=arm_config(read(package/'results/input360/config.json'),arm,read(folder/'manifest.json'))
    runtime.run('new-tfs' if arm in ('tfs-a01','tfs-a1') else 'old-tfs' if arm=='old-tfs' else 'immediate',smoke=smoke)
    result=audit(folder,arm,smoke,run_name)
    print(json.dumps(result,ensure_ascii=False),flush=True)


def main(args):
    if args.case!='all':
        args.case=MANAGER.resolve_case(args.case)
        if args.case not in CASES:
            raise SystemExit(f'{args.case} 当前仅支持校验、导出；运行合同尚未移植。请用 benchmark/manage.py export 导出，见 benchmark/benchmark目录.md。')
    overlay=args.dynamo_python.resolve()
    assert (overlay/'dynamo/_core.abi3.so').is_file(),'Build the weighted Dynamo overlay first'
    assert (overlay.parent/'build.json').is_file(),'Overlay build receipt is required'
    if args._execute:
        execute(args._execute.resolve(),args.arm,overlay,args.smoke,args._run_name);return
    assert (ROOT/'.venv').is_dir() and (ROOT/'upstream').is_dir(),'Bootstrap dependencies first'
    verified=MANAGER.verify()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    write(output/'benchmark.verify.json',verified)
    cases=CASES if args.case=='all' else (args.case,)
    arms=DEFAULT_ARMS if args.arm=='all' else tuple(ARMS) if args.arm=='all-five' else (args.arm,)
    layout=None
    if args.disks is not None or args.disk_gbps is not None:
        assert args.disks is not None and args.disk_gbps is not None
        layout=dict(disks=args.disks,disk_Bps=args.disk_gbps*1e9)
    for case in cases:prepare(case,output/case,overlay,layout,args.storage_backend)
    jobs=[dict(case=c,arm=a) for c in cases for a in arms]
    random.Random(args.seed).shuffle(jobs)
    write(output/'plan.json',dict(seed=args.seed,jobs=jobs,smoke=args.smoke,repeats=1,K=1,
        preparation='72 warmups in three phases; warm history excluded; formal GC window reset',
        platform=sys.platform,python=sys.version,native_sha256=sha(overlay/'dynamo/_core.abi3.so')))
    completed=[]
    for job in jobs:
        cmd=[sys.executable,str(__file__),'--_execute',str(output/job['case']),'--arm',job['arm'],
             '--dynamo-python',str(overlay)]+(['--smoke'] if args.smoke else [])
        print(json.dumps(dict(event='start',**job)),flush=True)
        with (output/f"{job['case']}-{job['arm']}.log").open('w') as log:
            child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
            try:code=child.wait()
            except BaseException:
                child.send_signal(signal.SIGINT)
                try:child.wait(timeout=30)
                except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=10)
                raise
        entry=dict(**job,returncode=code)
        if not code:entry.update(read(output/job['case']/f"package/results/{job['arm']}/run/benchmark.check.json"))
        completed.append(entry)
        status='failed' if code else 'completed' if len(completed)==len(jobs) else 'running'
        write(output/'progress.json',dict(status=status,completed=completed,total=len(jobs)))
        with (output/'summary.csv').open('w',newline='') as f:
            fields=list(dict.fromkeys(k for row in completed for k in row))
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(completed)
        print(json.dumps(dict(event='finish',**entry)),flush=True)
        if code:raise SystemExit(code)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',default='mooncake_abc_360_4s_12_90GBps',help='Benchmark目录中的名称；all只选择当前支持完整运行的负载。')
    p.add_argument('--arm',choices=(*ARMS,'all','all-five'),default='direct')
    p.add_argument('--storage-backend',choices=['python','cpp'],default='python')
    p.add_argument('--disks',type=int,help='Independent HRW disks; requires --disk-gbps and unchanged total bandwidth')
    p.add_argument('--disk-gbps',type=float,help='Per-disk decimal GB/s; no global shared link')
    p.add_argument('--output',type=Path,default=ROOT/'results/benchmark_runs/run')
    p.add_argument('--dynamo-python',type=Path,required=True)
    p.add_argument('--smoke',action='store_true',help='Two instances, two members per available class, no warmup; correctness only')
    p.add_argument('--seed',type=int,default=20261029)
    p.add_argument('--_execute',type=Path,help=argparse.SUPPRESS)
    p.add_argument('--_run-name',help=argparse.SUPPRESS)
    main(p.parse_args())
