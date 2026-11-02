"""Exercise one frozen benchmark arm with the previously audited warmup/GC protocol.

Only run_benchmark.py initializes this module. Native picker, resource bookings,
timing formulas and layer barriers remain in the simulator modules.
"""
import argparse
import asyncio
import contextlib
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

import psutil

# Filled by run_benchmark.py from an isolated, frozen per-case package.
A = ROOT = OVERLAY = None
CASE_NAME = None
ALPHA = 1.0
ARM_CONFIG = None


def smoke_requests(rows, long_context=False):
    """Retain class extremes; AgentX additionally exercises median contexts."""
    selected=[]
    for label in sorted({r['class_id'] for r in rows}):
        members=sorted((r for r in rows if r['class_id']==label),key=lambda r:len(r['prompt_token_ids']))
        indices={0,len(members)-1}
        if long_context:indices.add((len(members)-1)//2)
        for i in sorted(indices):
            selected.append(dict(members[i],arrival_s=0.))
    return selected


def read(p):
    return json.loads(Path(p).read_text())


def write(p, value):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def environment():
    os.environ.update(PYTHONHASHSEED='0', VLLM_PLUGINS='', HF_HUB_OFFLINE='1',
                      OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
                      VLLM_ALLOW_LONG_MAX_MODEL_LEN='1', VLLM_LOGGING_LEVEL='ERROR',
                      DYN_TCP_RPC_HOST='127.0.0.1')
    for key in ('SIM_FINE_TRACE_DIR', 'DYN_WAITING_SCORE_FACTOR', 'DYN_WAITING_SCORE_READY'):
        os.environ.pop(key, None)
    sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]


def run(arm, smoke=False):
    environment(); os.chdir(ROOT)
    import httpx
    import replay_http
    import run_tfs_stack
    from check_stack_routing import metrics_drained
    from sim.request_profile import normalize_config
    # Read-only post-run admission snapshots may include tens of thousands of
    # layer/permit facts. Extend only diagnostic GET timeouts; runtime permit,
    # storage and routing RPC timeouts and measured request paths are unchanged.
    original_request_json=run_tfs_stack.request_json
    def diagnostic_json(url):
        if '/sim/admission' not in url:return original_request_json(url)
        with urllib.request.urlopen(url,timeout=30) as response:return json.load(response)
    run_tfs_stack.request_json=diagnostic_json
    case = ROOT/'results'/CASE_NAME
    case.mkdir(exist_ok=False)
    from sim.trace_io import load_trace
    main = load_trace(ROOT/'results/input360/trace.jsonl')
    cfg = deepcopy(ARM_CONFIG) if ARM_CONFIG is not None else read(ROOT/'results/input360/config.json')
    contract_path=ROOT/'results/input360/run_contract.json'
    contract=read(contract_path) if contract_path.exists() else {}
    startup=contract.get('startup',{})
    new = arm in ('new-tfs','idle-bypass')
    if ARM_CONFIG is None:
        cfg['admission'].update(mode='immediate' if arm=='immediate' else 'tfs',
                                placement='scheduler' if new else 'gateway',
                                classification='arrival' if new else 'dynamic',
                                idle_bypass=arm=='idle-bypass',aging_enabled=False)
        cfg.update(policy='priority',request_ordering='edf')
        cfg['routing_accounting'].update(enabled=new)
        if new or contract.get('runner_profile') not in ('agentx360_v1','synthetic_abc360_v1'):
            cfg['routing_accounting']['waiting_factor']=ALPHA
    if smoke:
        main = smoke_requests(main, long_context=cfg['compute'].get('context_length_limit',131072)>131072)
        cfg['workers'] = 2
    cfg = normalize_config(cfg)
    warm=[]; phases=[]
    if not smoke:
        ordered=sorted(main,key=lambda r:len(r['prompt_token_ids']))
        for name, q in [('small',.1),('median',.5),('large',.9)]:
            template=ordered[int((len(ordered)-1)*q)]
            ids=[]
            for i in range(24):
                row=dict(template,id=f'warmup-{name}-{i:02d}',arrival_s=0.)
                warm.append(row);ids.append(row)
            phase=case/f'warmup-{name}.jsonl';phase.write_text(''.join(json.dumps(r)+'\n' for r in ids));phases.append((name,phase))
    measured=case/'measured_trace.jsonl';measured.write_text(''.join(json.dumps(r)+'\n' for r in main))
    combined=case/'trace.jsonl';combined.write_text(''.join(json.dumps(r)+'\n' for r in warm+main))
    write(case/'effective_config.json',cfg)
    for name in ('gateway.py','storage_transport.py'):
        shutil.copy2(ROOT/'scripts/benchmark_support'/name,case/name)
    for port in (28765,28766):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);probe.bind(('127.0.0.1',port))
    original_popen=subprocess.Popen;original_client=httpx.AsyncClient;original_replay=replay_http.replay
    original_verify=run_tfs_stack.verify;original_killpg=os.killpg
    trace_dirs=set();storage_path=[None];phase_state=['startup']
    def safe_killpg(pid,sig):
        try:return original_killpg(pid,sig)
        except PermissionError:
            try:os.kill(pid,sig)
            except ProcessLookupError:pass
    os.killpg=safe_killpg
    def launch(argv,*args,**kw):
        if argv[1:3]==['-m','sim.stack.admission_service']:
            argv=[argv[0],str(case/'gateway.py'),*argv[3:]]
        elif argv[1:3]==['-m','sim.stack.storage_service']:
            argv=[argv[0],str(case/'storage_transport.py'),*argv[3:]]
        env=kw.get('env',{})
        if env.get('SIM_STACK_CONFIG'):
            folder=Path(env['SIM_STACK_CONFIG']).parent/'fine_trace';folder.mkdir(exist_ok=True);trace_dirs.add(folder)
            kw['env']={**env,'SIM_FINE_TRACE_DIR':str(folder)}
            if env.get('SIM_STACK_STORAGE_SOCKET'):storage_path[0]=env['SIM_STACK_STORAGE_SOCKET']
            if argv[1].endswith('gateway.py'):
                argv=[argv[0],str(ROOT/'scripts/benchmark_support/controlled_gc_gateway.py'),
                      *([] if smoke else [str(ROOT/'scripts/benchmark_support/warmup_gateway.py')]),*argv[1:]]
            elif argv[1].endswith('storage_transport.py'):
                argv=[argv[0],'-m','sim.stack.fine_trace','storage',*argv[1:]]
        return original_popen(argv,*args,**kw)
    class Client(original_client):
        def __init__(self,*args,**kw):
            kw.update(timeout=600,limits=httpx.Limits(max_connections=512,max_keepalive_connections=64));super().__init__(*args,**kw)
    def control(action):
        url='http://127.0.0.1:28766/diagnostic/'+('warmup/finish' if action=='warmup/finish' else 'gc/'+action)
        req=urllib.request.Request(url,data=b'',method='POST')
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    async def replay(trace,url,model,out,lossy,api,**kwargs):
        from transformers import AutoTokenizer
        tokenizer=AutoTokenizer.from_pretrained(str(ROOT/'configs/model'));cache={}
        for r in warm+main:
            ids=r['prompt_token_ids'];key=(len(ids),ids[0])
            if key not in cache:
                text=tokenizer.decode(ids);assert tokenizer.encode(text)==ids;cache[key]=(ids,text)
            else:assert cache[key][0]==ids
        class PreparedTokenizer:
            def decode(self,ids,*a,**k):
                known,text=cache[len(ids),ids[0]];assert known==list(ids);return text
            def encode(self,text,*a,**k):
                for ids,known in cache.values():
                    if text==known:return ids
                raise ValueError('unexpected prompt')
            def __getattr__(self,key):return getattr(tokenizer,key)
        loader=AutoTokenizer.from_pretrained;AutoTokenizer.from_pretrained=lambda *a,**k:PreparedTokenizer()
        client_type=httpx.AsyncClient;reports=[]
        class BorrowedClient:
            def __init__(self,*a,**k):pass
            async def __aenter__(self):return shared
            async def __aexit__(self,*a):return False
        try:
            async with client_type(timeout=600,trust_env=False) as shared:
                httpx.AsyncClient=BorrowedClient
                for name,path in phases:
                    phase_state[0]='warmup-'+name;dest=out/('warmup-'+name);dest.mkdir()
                    assert await original_replay(path,url,model,dest,lossy,api,**kwargs)==0
                    report=read(dest/'report.json');assert report['n_ok']==24;reports.append(report)
                if phases:
                    limit=time.monotonic()+30
                    while True:
                        state=run_tfs_stack.request_json('http://127.0.0.1:28766/sim/admission')
                        records=state['records']
                        if (len(records)==72 and all(r['state']=='FINISHED' and not r['lease_open'] for r in records.values())
                                and not state['snapshot']['pending_ids'] and metrics_drained(run_tfs_stack.router_metrics())):break
                        if time.monotonic()>limit:raise TimeoutError('warmup drain')
                        await asyncio.sleep(.02)
                    # Online records no longer receive executor load/worker facts.
                    # Warmup has drained; join local logs only for this audit.
                    from sim.stack.load_observation import with_local_observations
                    observed=with_local_observations(state,sorted(out.glob('worker*.loads.jsonl')))
                    coverage=sorted({r['worker_id'] for r in observed['records'].values()})
                    assert coverage==list(range(cfg['workers'])),coverage
                    from sim.stack.storage_client import StorageClient
                    storage=StorageClient(storage_path[0]);snap=storage.stats();storage.close();assert snap['pending_reads']==0
                    history=control('warmup/finish');assert history['status']=='pass'
                    storage=StorageClient(storage_path[0]);storage.gc_end(collect=False)
                    boundary=storage.gc_begin();storage.close()
                    write(out/'storage.gc.formal.begin.json',boundary)
                    write(out/'warmup.check.json',dict(status='pass',requests=72,coverage=coverage,pending_reads=0,
                          threshold_reset=True,shared_http_client=True,prefix_cache=False))
                phase_state[0]='measured';dest=out/'measured';dest.mkdir();control('disable')
                print(f'{arm}: warmup drained; measured {len(main)} starts',flush=True)
                try:assert await original_replay(measured,url,model,dest,lossy,api,**kwargs)==0
                finally:control('restore')
                main_report=read(dest/'report.json');assert main_report['n_ok']==len(main);reports.append(main_report)
                phase_state[0]='verification'
        finally:httpx.AsyncClient=client_type;AutoTokenizer.from_pretrained=loader
        requests={rid:r for report in reports for rid,r in report['requests'].items()};assert len(requests)==len(warm+main)
        entry=next(iter(reports[0]['requests'].values()));origin=entry['sent_perf_s']-entry['actual_arrival_s']
        for report in reports:
            first=next(iter(report['requests'].values()));phase_origin=first['sent_perf_s']-first['actual_arrival_s']
            for r in report['requests'].values():
                r['actual_arrival_s']=r['sent_perf_s']-origin;r['planned_arrival_s']+=phase_origin-origin
        all_report=dict(main_report,n_total=len(requests),n_ok=len(requests),n_skipped=0,requests=requests,
                        wall_clock_s=time.perf_counter()-origin,model_elapsed_s=time.perf_counter()-origin,
                        warmup_requests=len(warm),measured_requests=len(main),
                        ttft_mean_s=sum(r['ttft_s'] for r in requests.values())/len(requests),
                        scope='Audit including warmup; measured/report.json is formal only')
        write(out/'report.json',all_report);return 0
    def verify(*args,**kwargs):
        for folder in trace_dirs:
            people=[read(f) for f in folder.glob('ready-*.json')]
            for person in people:os.kill(person['pid'],signal.SIGUSR1)
            limit=time.monotonic()+20
            while any(not (folder/f"flushed-{person['pid']}.json").exists() for person in people):
                if time.monotonic()>limit:raise TimeoutError('fine trace flush')
                time.sleep(.05)
            write(folder/'flush_check.json',dict(status='pass',processes=people))
        return original_verify(*args,**kwargs)
    parent=psutil.Process();stopped=threading.Event();samples=[];captured={};guards=[];started=time.monotonic()
    def monitor():
        low=None
        with (case/'resources.jsonl').open('w',buffering=1) as stream:
            while not stopped.is_set():
                rss=0
                for process in parent.children(recursive=True):
                    try:captured[process.pid]=process.create_time();rss+=process.memory_info().rss
                    except psutil.Error:pass
                sample=dict(elapsed_s=time.monotonic()-started,phase=phase_state[0],rss=rss,
                            available=psutil.virtual_memory().available,swap=psutil.swap_memory().used)
                samples.append(sample);stream.write(json.dumps(sample)+'\n')
                low=(low or time.monotonic()) if sample['available']<768*2**20 else None
                if not guards and ((low and time.monotonic()-low>10) or sample['elapsed_s']>900):
                    guards.append(sample);os.kill(os.getpid(),signal.SIGINT)
                stopped.wait(1)
    result=dict(status='failed',arm=arm,smoke=smoke);thread=threading.Thread(target=monitor,daemon=True)
    subprocess.Popen=launch;httpx.AsyncClient=Client;replay_http.replay=replay;run_tfs_stack.verify=verify;thread.start()
    try:
        with (case/'driver.log').open('w') as stream,contextlib.redirect_stdout(stream):
            options=({'admission_startup_timeout_s':startup['admission_ready_timeout_wall_s']}
                     if startup else {})
            check=run_tfs_stack.run_one(cfg,combined,case/'run',Path(sys.executable),cfg['admission']['mode'],'first_token',cfg['workers'],**options)
        assert check['requests']==len(warm+main)
        assert check['workers']==list(range(cfg['workers'])),check['workers']
        assert check['actual_submitted_read_bytes']==sum(r['hit_tokens']*cfg['storage']['kv_bytes_per_token'] for r in warm+main)
        if cfg['request_ordering']=='edf':
            from check_edf_priority import check as budget_check
            write(case/'run/edf_priority.check.json',budget_check(case/'run'))
        if new:
            from check_routing_accounting import check as score_check
            write(case/'run/routing.score.check.json',score_check(case/'run',require_discount=ALPHA<1))
        result.update(status='passed',check=check)
    except BaseException:
        result['error']=traceback.format_exc();raise
    finally:
        stopped.set();thread.join(5)
        subprocess.Popen=original_popen;httpx.AsyncClient=original_client;replay_http.replay=original_replay
        run_tfs_stack.verify=original_verify;os.killpg=original_killpg
        run_tfs_stack.request_json=original_request_json
        alive=[]
        for pid,created in captured.items():
            try:
                proc=psutil.Process(pid)
                if proc.create_time()==created and proc.status()!=psutil.STATUS_ZOMBIE:alive.append(pid)
            except psutil.Error:pass
        result.update(wall_s=time.monotonic()-started,guards=guards,alive=alive,
                      min_available_gib=min(s['available'] for s in samples)/2**30,
                      peak_children_rss_gib=max(s['rss'] for s in samples)/2**30,
                      max_swap_gib=max(s['swap'] for s in samples)/2**30)
        if guards or alive:result['status']='failed'
        write(case/'driver.json',result)
    assert result['status']=='passed'
    print(json.dumps(dict(phase='completed',arm=arm,smoke=smoke,wall_s=result['wall_s'],requests=len(main))),flush=True)
