"""Portable K=1 benchmark runner: frozen inputs, 72 warmups, native stack and audits."""
import argparse
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
REPO=ROOT.parent
ARMS={'direct':1.,'tfs-a01':.1,'tfs-a1':1.}
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


def prepare(case,folder,overlay):
    package=folder/'package';package.mkdir(parents=True)
    for name in ('sim','scripts','configs','patched'):
        shutil.copytree(ROOT/name,package/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('versions.json','source-fingerprints.json'):shutil.copy2(ROOT/name,package/name)
    for name in ('.venv','upstream'):(package/name).symlink_to((ROOT/name).resolve(),target_is_directory=True)
    inp=package/'results/input360'
    MANAGER.export(case,inp,quiet=True)
    manifest=read(inp/'manifest.json');assert sha(inp/'trace.jsonl')==manifest['original_trace_sha256']
    cfg=read(inp/'config.json');cfg['routing_accounting']['python_path']=str(overlay)
    assert cfg['wall_time_scale']==1 and cfg['workers']==12
    assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==90e9
    assert cfg['admission']['reference_bandwidth_Bps']==90e9
    write(inp/'config.json',cfg)
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
        native_build_sha256=sha(overlay.parent/'build.json')))


def audit(folder,arm,smoke):
    run=folder/f'package/results/{arm}/run';cfg=read(run/'config.json');state=read(run/'admission.final.json')
    driver=read(run.parent/'driver.json');client=read(run/'measured/report.json')
    records={k:v for k,v in state['records'].items() if k.startswith('conversation:')}
    profiles={r['id']:r for r in read(folder/'profiles.json')};manifest=read(folder/'manifest.json')
    assert driver['status']=='passed' and not driver['guards'] and not driver['alive']
    assert len(records)==client['n_total']==client['n_ok'] and not client['n_skipped']
    assert not state['fatal'] and not state['invalid_reasons'] and not state['pending_engine_events']
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
    assert cfg['wall_time_scale']==1 and cfg['storage']['disk_Bps']==90e9
    assert cfg['request_ordering']=='edf'
    assert cfg['admission']['idle_bypass'] is False
    if arm!='direct':
        assert cfg['admission']['placement']=='scheduler' and cfg['admission']['classification']=='arrival'
        assert cfg['routing_accounting']['waiting_factor']==ARMS[arm]
    ts=sorted(ttft);n=len(ts)
    result=dict(status='passed',case=manifest['case'],arm=arm,smoke=smoke,requests=n,
        warmup_requests=0 if smoke else 72,K=1,shared_storage_Bps=90e9,
        mean_ttft_ms=sum(ts)/n,p95_ttft_ms=ts[math.ceil(n*.95)-1],slo_pct=slo/n*100,
        completion_ms=(max(r['first_token_s'] for r in records.values())-min(r['profile']['arrival_s'] for r in records.values()))*1000,
        measured_read_bytes=sum(r['submitted_read_bytes'] for r in records.values()))
    write(run/'benchmark.check.json',result)
    return result


def execute(folder,arm,overlay,smoke):
    frozen=read(folder/'frozen.json');package=folder/'package'
    assert all(sha(package/name)==expected for name,expected in frozen['files'].items())
    assert sha(package/'results/input360/trace.jsonl')==frozen['input_sha256']
    assert sha(package/'results/input360/config.json')==frozen['config_sha256']
    assert sha(overlay/'dynamo/_core.abi3.so')==frozen['native_sha256']
    assert sha(overlay.parent/'build.json')==frozen['native_build_sha256']
    sys.path[:0]=[str(package),str(package/'scripts')]
    runtime=load_module('benchmark_runtime',package/'scripts/benchmark_runtime.py')
    runtime.A=folder;runtime.ROOT=package;runtime.OVERLAY=overlay
    runtime.CASE_NAME=arm;runtime.ALPHA=ARMS[arm]
    runtime.run('immediate' if arm=='direct' else 'new-tfs',smoke=smoke)
    result=audit(folder,arm,smoke)
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
        execute(args._execute.resolve(),args.arm,overlay,args.smoke);return
    assert (ROOT/'.venv').is_dir() and (ROOT/'upstream').is_dir(),'Bootstrap dependencies first'
    verified=MANAGER.verify()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    write(output/'benchmark.verify.json',verified)
    cases=CASES if args.case=='all' else (args.case,)
    arms=tuple(ARMS) if args.arm=='all' else (args.arm,)
    for case in cases:prepare(case,output/case,overlay)
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
    p.add_argument('--arm',choices=(*ARMS,'all'),default='direct')
    p.add_argument('--output',type=Path,default=ROOT/'results/benchmark_runs/run')
    p.add_argument('--dynamo-python',type=Path,required=True)
    p.add_argument('--smoke',action='store_true',help='Two instances, two members per available class, no warmup; correctness only')
    p.add_argument('--seed',type=int,default=20261029)
    p.add_argument('--_execute',type=Path,help=argparse.SUPPRESS)
    main(p.parse_args())
