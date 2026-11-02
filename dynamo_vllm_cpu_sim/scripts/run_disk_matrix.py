"""Six frozen inputs, independent HRW disks, five native scheduling/admission arms."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import run_benchmark as runner

LAYOUTS={
    'agentx_random_360_12s_12_180GBps':(6,30),
    'mooncake_abc_360_4s_12_100GBps':(4,25),
    'mooncake_abcd_360_4s_12_100GBps':(4,25),
    'mooncake_abc_360_4s_12_90GBps':(3,30),
    'mooncake_abcd_360_4s_12_90GBps':(3,30),
    'synthetic_abc_360_6s_12_90GBps':(3,30),
}
SMOKES=dict(zip(LAYOUTS,('tfs-a01','direct-fcfs','old-tfs','tfs-a1','direct','old-tfs')))


def stamp():return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def main(args):
    output=args.output.resolve();overlay=args.dynamo_python.resolve()
    if not args.resume:
        output.mkdir(parents=True,exist_ok=False)
        verified=runner.MANAGER.verify();runner.write(output/'benchmark.verify.json',verified)
        for case,(n,speed) in LAYOUTS.items():
            runner.prepare(case,output/case,overlay,dict(disks=n,disk_Bps=speed*1e9))
        jobs=[dict(case=c,arm=a) for c in LAYOUTS for a in runner.ARMS]
        random.Random(args.seed).shuffle(jobs)
        runner.write(output/'plan.json',dict(created_at=stamp(),seed=args.seed,jobs=jobs,
            repeats=1,formal_requests=10800,warmup_requests=2160,arms=list(runner.ARMS),
            layouts={c:dict(disks=n,disk_GBps=s,shared_link_Bps=None) for c,(n,s) in LAYOUTS.items()},
            ordering='FCFS only for direct-fcfs; EDF for other four arms',
            scope='independent disks, HRW placement, per-disk max-min; global scalar TFS admission'))
        runner.write(output/'attempts.json',[])
    plan=runner.read(output/'plan.json');attempts=runner.read(output/'attempts.json')
    smoke_jobs=[dict(case=c,arm=a,smoke=True) for c,a in SMOKES.items()]
    jobs=smoke_jobs+[dict(**j,smoke=False) for j in plan['jobs']]
    for job in jobs:
        past=[r for r in attempts if all(r[k]==job[k] for k in job)]
        if any(r['returncode']==0 for r in past):continue
        for attempt in range(len(past)+1,4):
            run_name=('smoke-' if job['smoke'] else '')+job['arm']+(f'-r{attempt}' if attempt>1 else '')
            logfile=output/f"{job['case']}-{run_name}.log"
            entry=dict(**job,attempt=attempt,run_name=run_name,started_at=stamp())
            runner.write(output/'current.json',dict(status='running',**entry))
            print(json.dumps(dict(event='start',**entry)),flush=True)
            cmd=[sys.executable,str(Path(runner.__file__)),'--_execute',str(output/job['case']),
                 '--arm',job['arm'],'--_run-name',run_name,'--dynamo-python',str(overlay)]
            if job['smoke']:cmd.append('--smoke')
            with logfile.open('w') as log:
                proc=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
            run=output/job['case']/f'package/results/{run_name}/run'
            entry.update(returncode=proc.returncode,finished_at=stamp(),log=logfile.name)
            driver=run.parent/'driver.json'
            if driver.exists():entry['driver']=runner.read(driver)
            if proc.returncode==0:entry['check']=runner.read(run/'benchmark.check.json')
            attempts.append(entry);runner.write(output/'attempts.json',attempts)
            accepted=[r for r in attempts if r['returncode']==0 and not r['smoke']]
            runner.write(output/'progress.json',dict(status='completed' if len(accepted)==30 else 'running',
                accepted_formal=len(accepted),accepted_smokes=sum(r['returncode']==0 and r['smoke'] for r in attempts),
                failures=sum(r['returncode']!=0 for r in attempts),total_formal=30))
            print(json.dumps(dict(event='finish',case=job['case'],arm=job['arm'],smoke=job['smoke'],
                attempt=attempt,returncode=proc.returncode,accepted_formal=len(accepted))),flush=True)
            if proc.returncode==0:break
            # Retry only recognized transient transport/startup failures, never assertions.
            text=logfile.read_text(errors='replace')
            transient=any(s in text for s in ('stack readiness timed out','Network is unreachable','os error 101'))
            cleaned=entry.get('driver',{}).get('alive')==[] and not entry.get('driver',{}).get('guards')
            if not transient or not cleaned or attempt==3:raise RuntimeError('retained failed attempt requires diagnosis: '+str(logfile))
            time.sleep(2)
    assert runner.read(output/'progress.json')['accepted_formal']==30
    runner.write(output/'current.json',dict(status='completed',finished_at=stamp()))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--dynamo-python',type=Path,required=True)
    p.add_argument('--seed',type=int,default=20261102);p.add_argument('--resume',action='store_true')
    main(p.parse_args())
