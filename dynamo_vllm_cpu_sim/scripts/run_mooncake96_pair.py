"""Run one frozen Mooncake96 arm, with native memory guard and archived adapters."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback

import psutil
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
os.environ.update(PYTHONHASHSEED='0',VLLM_PLUGINS='',HF_HUB_OFFLINE='1',
    OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',
    VLLM_ALLOW_LONG_MAX_MODEL_LEN='1',VLLM_LOGGING_LEVEL='ERROR')


def write(p,o):p.write_text(json.dumps(o,indent=2)+'\n')


def main(a):
    source=ROOT/'results/mooncake96_4s_source_20261026'
    c=json.loads((source/'config.json').read_text());c['admission']['mode']=a.mode
    rows=[json.loads(s) for s in (source/'trace.jsonl').read_text().splitlines()]
    if a.smoke:
        rows=[dict(r,arrival_s=0.) for r in rows[:6]];c['workers']=2
    out=ROOT/'results'/a.name;out.mkdir(exist_ok=False)
    trace=out/'trace.jsonl';trace.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    write(out/'config.json',c)
    files=[*sorted((ROOT/'sim').rglob('*.py')),Path(__file__),ROOT/'scripts/run_tfs_stack.py',ROOT/'scripts/stack_vllm_worker.py']
    snapshot=out/'runtime_sources'
    for p in files:
        dest=snapshot/p.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(p.read_bytes())
    write(out/'provenance.json',dict(form=a.form,mode=a.mode,smoke=a.smoke,requests=len(rows),workers=c['workers'],
        frozen_trace_sha256=hashlib.sha256((source/'trace.jsonl').read_bytes()).hexdigest(),
        effective_trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        sources={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        fine_trace=False,storage='Same GroupedStorage counted per-request layer submissions in both forms'))
    started=time.monotonic();result=dict(form=a.form,mode=a.mode,requests=len(rows))
    if a.form=='des':
        from sim.layer_aligned import simulate
        import asyncio
        report=asyncio.run(simulate(c,rows,out))
        result.update(status='pass',metrics={k:report[k] for k in ('requests_completed','makespan_s','mean_ttft_s','profile_deadline_attainment','bytes_read')})
    else:
        from run_tfs_stack import run_one
        import httpx
        for name in ('gateway.py','storage_transport.py'):
            shutil_source=ROOT/'results/concurrency_original_step5_20261024'/name
            (out/name).write_bytes(shutil_source.read_bytes())
        original_popen,original_client=subprocess.Popen,httpx.AsyncClient
        def popen(argv,*args,**kwargs):
            if argv[1:3]==['-m','sim.stack.admission_service']:argv=[argv[0],str(out/'gateway.py'),*argv[3:]]
            elif argv[1:3]==['-m','sim.stack.storage_service']:argv=[argv[0],str(out/'storage_transport.py'),*argv[3:]]
            return original_popen(argv,*args,**kwargs)
        class Client(original_client):
            def __init__(self,*args,**kwargs):
                kwargs.update(timeout=600*c['wall_time_scale'],limits=httpx.Limits(max_connections=512,max_keepalive_connections=64))
                super().__init__(*args,**kwargs)
        subprocess.Popen,httpx.AsyncClient=popen,Client
        parent=psutil.Process();captured={};samples=[];guards=[];stopped=threading.Event()
        def monitor():
            low=None
            with (out/'resources.jsonl').open('w',buffering=1) as stream:
                while not stopped.is_set():
                    children=[]
                    for process in parent.children(recursive=True):
                        try:
                            info=dict(pid=process.pid,created=process.create_time(),rss=process.memory_info().rss)
                            captured[process.pid]=info;children.append(info)
                        except psutil.Error:pass
                    item=dict(elapsed_s=time.monotonic()-started,available=psutil.virtual_memory().available,
                        swap=psutil.swap_memory().used,rss=sum(x['rss'] for x in children))
                    samples.append(item);stream.write(json.dumps(item)+'\n')
                    low=(low or time.monotonic()) if item['available']<768*2**20 else None
                    if not guards and ((low and time.monotonic()-low>10) or item['elapsed_s']>1800):
                        guards.append(item);os.kill(os.getpid(),signal.SIGINT)
                    stopped.wait(1)
        thread=threading.Thread(target=monitor,daemon=True);thread.start()
        try:
            check=run_one(c,trace,out/'run',Path(sys.executable),a.mode,'first_token',c['workers'])
            if not a.smoke:assert check['workers']==list(range(12)),check['workers']
            assert check['actual_submitted_read_bytes']==sum(r['hit_tokens']*109824 for r in rows)
            state=json.loads((out/'run/admission.final.json').read_text())
            rr=list(state['records'].values())
            result.update(status='pass',metrics=dict(requests_completed=len(rr),
                mean_ttft_s=sum(r['first_token_s']-r['profile']['arrival_s'] for r in rr)/len(rr),
                makespan_s=max(r['first_token_s'] for r in rr)-min(r['profile']['arrival_s'] for r in rr),
                profile_deadline_attainment=sum(r['first_token_s']<=r['profile']['deadline_s'] for r in rr)/len(rr),
                bytes_read=check['actual_submitted_read_bytes']),check=check)
        except BaseException:
            result.update(status='failed',error=traceback.format_exc());raise
        finally:
            stopped.set();thread.join(5)
            subprocess.Popen,httpx.AsyncClient=original_popen,original_client
            leftovers=[]
            for pid,info in captured.items():
                try:
                    process=psutil.Process(pid)
                    if process.create_time()==info['created'] and process.status()!=psutil.STATUS_ZOMBIE:
                        leftovers.append(pid);process.terminate()
                except psutil.Error:pass
            if leftovers:psutil.wait_procs([psutil.Process(p) for p in leftovers if psutil.pid_exists(p)],timeout=8)
            result.update(guard_events=guards,leftovers_before_fallback=leftovers,
                min_available_gib=min(s['available'] for s in samples)/2**30,
                max_swap_gib=max(s['swap'] for s in samples)/2**30,
                peak_children_rss_gib=max(s['rss'] for s in samples)/2**30,wall_s=time.monotonic()-started)
            write(out/'driver_result.json',result)
            assert not guards and not leftovers
    result['wall_s']=time.monotonic()-started;write(out/'driver_result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='check'}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--form',choices=['native','des'],required=True)
    p.add_argument('--mode',choices=['immediate','tfs'],required=True);p.add_argument('--name',required=True)
    p.add_argument('--smoke',action='store_true');main(p.parse_args())
