"""GLM DES with the same per-request/per-layer grouped pool as the native stack.

Only execution and wall-clock plumbing differ. Native vLLM scheduling, request
KV allocation, compute formula, storage waterfill and TFS remain authoritative.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import statistics

os.environ.setdefault('VLLM_PLUGINS','')
os.environ.setdefault('HF_HUB_OFFLINE','1')
os.environ.setdefault('VLLM_ALLOW_LONG_MAX_MODEL_LEN','1')
os.environ.setdefault('VLLM_LOGGING_LEVEL','ERROR')

from sim.engine import Engine,ModelRunnerOutput
from sim.sa_layer_engine import make_sa_request
from sim.grouped_storage import GroupedStorage
from sim.compute_time import estimate_step
from sim.stack.layerwise import layer_spec,layer_durations
from sim.request_profile import normalize_config,build_profile
from sim.admission import AdmissionController
from sim.control_events import ControlEvent
from sim.router import Router


class LayerEngine(Engine):
    def __init__(self,wid,c,store,rows,profiles,emit,control):
        native=dict(c,connector_name='SALayerConnector')
        super().__init__(wid,native,store)
        self.scheduler.connector.remote={r['id']:r['hit_tokens'] for r in rows}
        self.profiles=profiles
        self.emit,self.control=emit,control
        self.runtime=None
        self.loads={}
        self.resident=set()
        self.reads={}
        self.layers=layer_spec(c)['layers']
        self.layer_bytes=layer_spec(c)['bytes_per_token']

    def submit_layer(self,layer,now):
        s=self.runtime
        pending=[rid for rid in s['ids'] if rid in self.loads and rid not in self.resident]
        s['pending'][layer]=set(pending)
        for rid in pending:
            h=self.loads[rid]
            key=(self.id,rid,layer)
            latency=self.c['storage']['read_latency_s'] if layer==0 else 0.
            demand=self.profiles[rid].pressure_Bps/h
            self.store.submit_counted(now,key,{0:h},self.layer_bytes,demand,latency)
            self.reads[key]=dict(worker=self.id,rid=rid,layer=layer,submitted_s=now,
                completed_s=None,bytes=h*self.layer_bytes)
            self.emit(dict(t=now,event='layer_read_submitted',worker=self.id,id=rid,
                step_id=s['step'],layer=layer,bytes=h*self.layer_bytes))
            self.control('layer_load_submitted',rid,self.id,dict(attempt_id=1,layer=layer,
                submitted_read_bytes=h*self.layer_bytes))

    def read_done(self,key,now):
        _,rid,layer=key
        self.reads[key]['completed_s']=now
        self.runtime['pending'][layer].remove(rid)
        self.emit(dict(t=now,event='layer_read_complete',worker=self.id,id=rid,
            step_id=self.runtime['step'],layer=layer))
        if layer==self.layers-1:
            self.resident.add(rid)
            self.control('load_completed',rid,self.id,dict(attempt_id=1))

    def schedule_layer(self,now):
        if self.runtime is not None or not self.scheduler.has_requests():return
        out=self.scheduler.schedule()
        if not out.num_scheduled_tokens:
            if self.scheduler.get_num_unfinished_requests()==0:
                self.scheduler.update_from_output(out,ModelRunnerOutput(req_ids=[],req_id_to_index={},sampled_token_ids=[]));return
            raise RuntimeError('No scheduler progress; KV capacity/preemption is outside this run')
        ids=list(out.num_scheduled_tokens)
        shapes=[];sampled=[]
        for rid,count in out.num_scheduled_tokens.items():
            req=self.scheduler.requests[rid]
            before=req.num_computed_tokens-count
            assert before+count<=req.num_prompt_tokens
            shapes.append((before,count))
            sampled.append([100] if req.num_computed_tokens>=req.num_tokens else [])
        for rid,h in self.scheduler.connector.submissions:
            assert rid not in self.loads,'Repeated/preempted load unsupported'
            self.loads[rid]=h
            self.control('load_submitted',rid,self.id,dict(attempt_id=1,submitted_read_bytes=0,
                expected_read_bytes=h*self.c['storage']['kv_bytes_per_token'],layer_count=self.layers))
        self.scheduler.connector.submissions.clear()
        for rid in ids:
            if rid not in self.loads:self.control('no_external_load',rid,self.id)
        timing=estimate_step(self.c['compute'],shapes)
        durations=layer_durations(timing,self.c)
        self.steps+=1
        self.runtime=dict(out=out,model=ModelRunnerOutput(req_ids=ids,req_id_to_index={r:i for i,r in enumerate(ids)},
            sampled_token_ids=sampled),ids=ids,step=self.steps,start=now,layer=0,phase='STALL',
            end=math.inf,durations=durations,pending={})
        self.emit(dict(t=now,event='step_started',worker=self.id,step_id=self.steps,
            num_scheduled_tokens=dict(out.num_scheduled_tokens),timing=timing,durations=durations))
        self.submit_layer(0,now)

    def close_layers(self,now):
        s=self.runtime
        if s is None:return None
        if s['phase']=='COMPUTE' and s['end']<=now:
            self.emit(dict(t=now,event='layer_compute_complete',worker=self.id,step_id=s['step'],layer=s['layer']))
            s['layer']+=1;s['phase']='STALL';s['end']=math.inf
            if s['layer']==self.layers:
                self.runtime=None
                return s['out'],s['model'],now
        if s['phase']=='STALL' and not s['pending'][s['layer']]:
            layer=s['layer'];duration=s['durations'][layer]
            s['phase']='COMPUTE';s['end']=now+duration
            self.emit(dict(t=now,event='layer_compute_started',worker=self.id,step_id=s['step'],
                layer=layer,end_s=s['end'],ids=s['ids'],duration_s=duration))
            if layer+1<self.layers:self.submit_layer(layer+1,now)
        return None

    def account(self,dt):
        if self.runtime is not None and self.runtime['phase']=='COMPUTE':self.compute_s+=dt
        elif self.scheduler.has_requests():self.stall_s+=dt
        else:self.idle_s+=dt


async def simulate(config,rows,output):
    c=normalize_config(config)
    assert c['compute']['mode']=='glm5_d950pr' and c['kv_transfer_mode']=='layer_pipeline'
    assert c['block_size']==1 and not c['enable_prefix_caching']
    assert c['storage']['disks']==1 and c['storage']['bandwidth_policy']=='max_min_fair'
    assert c['admission']['feedback_delay_s']==0 and not c['admission']['aging_enabled']
    assert all(r['output_tokens']==1 and r['u_tokens']==len(r['prompt_token_ids'])-r['hit_tokens'] for r in rows)
    rows=sorted(rows,key=lambda r:(r['arrival_s'],r['id']))
    byid={r['id']:r for r in rows}
    profiles={r['id']:build_profile(r,c) for r in rows}
    reqs={r['id']:make_sa_request({**r,'deadline_s':profiles[r['id']].deadline_s},
                                c['policy']) for r in rows}
    store=GroupedStorage(c['storage'],1)
    ctrl=AdmissionController(c['admission']);router=Router(c.get('router_mode','kv'))
    events=[];sequence=0;now=0.
    stats={r['id']:dict(arrival_s=r['arrival_s'],class_id=r['class_id'],
        h_tokens=r['hit_tokens'],u_tokens=r['u_tokens'],submitted_read_bytes=0,load_submit_s=None,load_done_s=None) for r in rows}
    def emit(ev):events.append(ev)
    def control(kind,rid,wid,payload=None):
        nonlocal sequence
        sequence+=1
        ev=ControlEvent(f'layer:{sequence}',kind,rid,wid,now,now,payload or {})
        ctrl.on_event(ev,now)
        emit(dict(t=now,event=kind,id=rid,worker=wid,**(payload or {})))
        if kind=='load_submitted':stats[rid]['load_submit_s']=now
        elif kind=='layer_load_submitted':stats[rid]['submitted_read_bytes']+=payload['submitted_read_bytes']
        elif kind=='load_completed':stats[rid]['load_done_s']=now
    engines=[LayerEngine(i,c,store,rows,profiles,emit,control) for i in range(c['workers'])]
    async def complete(e,result):
        nonlocal done
        for entry in e.complete(result):
            rid=entry.request_id
            if entry.new_token_ids:
                assert 'first_token_s' not in stats[rid]
                stats[rid]['first_token_s']=now;control('first_token',rid,e.id)
                await router.first_token(rid)
            if entry.finish_reason is not None:
                stats[rid]['finish_s']=now;stats[rid]['preemptions']=reqs[rid].num_preemptions
                control('request_finished',rid,e.id);await router.finish(rid);done+=1
        emit(dict(t=now,event='step_complete',worker=e.id,step_id=e.steps))
    cursor=done=0
    try:
        await router.start(c)
        for iteration in range(500000):
            for e in engines:
                result=e.close_layers(now)
                if result is not None:await complete(e,result)
            arrived=[]
            while cursor<len(rows) and rows[cursor]['arrival_s']<=now:
                arrived.append(profiles[rows[cursor]['id']]);cursor+=1
            ctrl.on_arrivals(arrived,now)
            while (decision:=ctrl.pick(now)) is not None:
                ctrl.reserve(decision,now);rid=decision.request_id
                wid=await router.route(byid[rid]);engines[wid].scheduler.add_request(reqs[rid])
                ctrl.mark_submitted(rid,wid,now);stats[rid].update(worker=wid,submitted_s=now)
            for e in engines:
                e.schedule_layer(now);e.close_layers(now)
            events.extend(ctrl.logs);ctrl.logs.clear()
            if done==len(rows):break
            target=min([rows[cursor]['arrival_s'] if cursor<len(rows) else math.inf,store.next_time()]
                +[e.runtime['end'] for e in engines if e.runtime])
            if not math.isfinite(target) or target<=now:raise RuntimeError(f'DES deadlock at {now}')
            for e in engines:e.account(target-now)
            rates=store._group_rates()
            actual=sum(rate*store.queues[p][0].block_count for p,rate in rates.items())
            demand=sum(r.demand*r.block_count for q in store.queues.values() for r in [q[0]] if r.ready<=now+1e-12)
            emit(dict(t=target,event='storage_interval',**{'from':now},actual_Bps=actual,demand_Bps=demand,
                active_flows=len(store.pending)))
            completed=store.advance(target);now=target
            for key in completed:engines[key[0]].read_done(key,now)
        else:raise RuntimeError('DES event guard')
        events.extend(ctrl.logs);ctrl.logs.clear()
        for rid,r in stats.items():
            record=ctrl.records[rid];r.update(deadline_s=record.profile.deadline_s,
                ttft_s=r['first_token_s']-r['arrival_s'],central_wait_s=r['submitted_s']-r['arrival_s'],
                lease_open=record.lease_open,state=record.state)
        expected=sum(r['hit_tokens']*c['storage']['kv_bytes_per_token'] for r in rows)
        assert abs(store.bytes_transferred-expected)<1. and not store.pending
        assert not ctrl.pending and not any(r.lease_open for r in ctrl.records.values())
        assert all(r['preemptions']==0 for r in stats.values())
        assert all(len(e.reads)==len(e.loads)*78 for e in engines)
        for e in engines:assert all(x['completed_s'] is not None for x in e.reads.values())
        report=dict(status='completed',requests_completed=done,makespan_s=now,
            mean_ttft_s=statistics.mean(r['ttft_s'] for r in stats.values()),
            profile_deadline_attainment=sum(r['first_token_s']<=r['deadline_s'] for r in stats.values())/len(rows),
            bytes_read=store.bytes_transferred,expected_read_bytes=expected,requests=stats,
            workers=[dict(id=e.id,compute_s=e.compute_s,stall_s=e.stall_s,idle_s=e.idle_s,steps=e.steps) for e in engines],
            read_history=[r for e in engines for r in e.reads.values()],
            router_loads_after_finish=router.service.loads(),config=c,
            scheduler_sha256=hashlib.sha256(Path(inspect.getfile(type(engines[0].scheduler))).read_bytes()).hexdigest(),
            scope='Native scheduler/router; same GLM compute and counted per-request layer pool as process stack; virtual control costs zero')
        out=Path(output);out.mkdir(parents=True,exist_ok=True)
        (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        (out/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        print(json.dumps({k:report[k] for k in ('requests_completed','makespan_s','mean_ttft_s','profile_deadline_attainment','bytes_read')}),flush=True)
        return report
    finally:await router.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--trace',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();c=json.loads(Path(a.config).read_text());rows=[json.loads(s) for s in Path(a.trace).read_text().splitlines()]
    asyncio.run(simulate(c,rows,a.output))
