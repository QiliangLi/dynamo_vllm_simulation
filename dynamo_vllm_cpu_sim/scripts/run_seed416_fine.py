"""One approved seed: buffered native timings and unchanged kv DES controls."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
os.environ.update(PYTHONHASHSEED='0',VLLM_PLUGINS='',HF_HUB_OFFLINE='1',
    OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',
    VLLM_ALLOW_LONG_MAX_MODEL_LEN='1',VLLM_LOGGING_LEVEL='ERROR')


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def native(name,mode,smoke,enabled):
    import run_sa_abc_stack as runner
    import run_tfs_stack
    original_popen=subprocess.Popen
    original_verify=run_tfs_stack.verify
    trace_dirs=set()
    def launch(argv,*a,**kw):
        env=kw.get('env',{})
        if enabled and env.get('SIM_STACK_CONFIG'):
            folder=Path(env['SIM_STACK_CONFIG']).parent/'fine_trace'
            folder.mkdir(exist_ok=True);trace_dirs.add(folder)
            kw['env']={**env,'SIM_FINE_TRACE_DIR':str(folder)}
            if argv[1].endswith('gateway.py'):
                argv=[argv[0],'-m','sim.stack.fine_trace','gateway',*argv[1:]]
            elif argv[1].endswith('storage_transport.py'):
                argv=[argv[0],'-m','sim.stack.fine_trace','storage',*argv[1:]]
        return original_popen(argv,*a,**kw)
    def verify(*a,**k):
        # Runs after first tokens, admission drain and storage final snapshot.
        # Only registered probe processes receive USR1; frontend is excluded.
        manifest=[]
        for folder in trace_dirs:
            for p in sorted(folder.glob('ready-*.json')):
                info=json.loads(p.read_text());os.kill(info['pid'],signal.SIGUSR1)
                manifest.append(info)
            deadline=time.monotonic()+15
            while any(not (folder/f"flushed-{x['pid']}.json").exists() for x in manifest):
                if time.monotonic()>deadline:raise TimeoutError('trace flush')
                time.sleep(.05)
            write(folder/'flush_check.json',dict(status='pass',processes=manifest))
        return original_verify(*a,**k)
    subprocess.Popen=launch
    run_tfs_stack.verify=verify
    try:
        runner.run(SimpleNamespace(output=ROOT/'results'/name,
            source=ROOT/'results/seed416_fine_source_20261026',name=mode,mode=mode,
            workers=12,scale=10.,router_mode='kv',kv_mode='layer_pipeline',smoke=smoke,
            wall_budget=1800.,min_available_mib=768.))
    finally:
        subprocess.Popen=original_popen;run_tfs_stack.verify=original_verify


def des(name,mode):
    from run_sa_alignment import configuration
    source=ROOT/'results/seed416_fine_source_20261026'
    base=json.loads((source/'config.json').read_text())
    trace=[json.loads(s) for s in (source/'trace.jsonl').read_text().splitlines()]
    rows=[dict(id=r['id'],sa_rid=r['sa_rid'],arrival_s=r['arrival_s'],class_id=r['class_id'],
        h_tokens=r['hit_tokens'],u_tokens=r['u_tokens'],T0_s=r['T0_s'],deadline_s=r['deadline_s']) for r in trace]
    out=ROOT/'results'/name
    out.mkdir(parents=True,exist_ok=False)
    tracepath=out/'trace.json';write(tracepath,rows)
    metadata=json.loads((ROOT/'results/sa_alignment_20261019/source/source.json').read_text())
    cfg=configuration(metadata,tracepath,4,mode)
    cfg.update(workers=12,router_mode='kv',ttft_slo_s=base['ttft_slo_s'])
    cfg['admission']={**base['admission'],'mode':mode}
    cfg['storage'].update(shared_link_Bps=60e9,q_max_Bps=200e9)
    write(out/'config.json',cfg)
    write(out/'provenance.json',dict(source_trace_sha256=hashlib.sha256((source/'trace.jsonl').read_bytes()).hexdigest(),
        logical_instances=12,process_form='single DES process',router_mode='kv',
        wall_scale='not applied to DES virtual time'))
    started=time.monotonic()
    with (out/'run.log').open('w') as stream:
        subprocess.run([sys.executable,'-m','sim.sa_aligned','--config',str(out/'config.json'),
            '--trace',str(tracepath),'--output',str(out)],cwd=ROOT,
            stdout=stream,stderr=subprocess.STDOUT,check=True)
    with (out/'check.log').open('w') as stream:
        subprocess.run([sys.executable,'scripts/check_sa_results.py',str(out)],cwd=ROOT,
            stdout=stream,stderr=subprocess.STDOUT,check=True)
    report=json.loads((out/'report.json').read_text())
    write(out/'driver_result.json',dict(status='pass',wall_s=time.monotonic()-started,
        metrics={k:report[k] for k in ('requests_completed','makespan_s','mean_ttft_s','profile_deadline_attainment','bytes_read')}))
    print(json.dumps(json.loads((out/'driver_result.json').read_text())),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--form',choices=['native','des'],required=True)
    p.add_argument('--name',required=True)
    p.add_argument('--mode',choices=['immediate','tfs'],required=True)
    p.add_argument('--smoke',action='store_true')
    p.add_argument('--no-trace',action='store_true')
    a=p.parse_args()
    if a.form=='native':native(a.name,a.mode,a.smoke,not a.no_trace)
    else:des(a.name,a.mode)
