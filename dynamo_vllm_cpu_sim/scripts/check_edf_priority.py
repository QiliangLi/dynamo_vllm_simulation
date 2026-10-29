"""Audit literal per-round EDF deadline calculations and the real native heap heads."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sim.stack.edf_priority import calculate,validate
from check_fine_trace import key


def check(folder):
    folder=Path(folder)
    state=json.loads((folder/'admission.final.json').read_text())
    records=state['records']
    aliases={key(e['engine_uuid']):e['id'] for e in state['events'] if e['event']=='request_correlated'}
    forwarded={e['id']:e['metadata'] for e in state['events'] if e['event']=='edf_priority_forwarded'}
    assert set(forwarded)==set(records)
    for rid,value in forwarded.items():
        validate(value)
        p=records[rid]['profile']
        for source,target in [('arrival_s','arrival_s'),('deadline_s','deadline_s'),('reference_service_s','reference_service_s')]:
            assert value[source]==p[target],(rid,source)
    ready={int(p.stem.split('-')[1]):json.loads(p.read_text()) for p in (folder/'fine_trace').glob('ready-*.json')}
    received={};policies={};counts=Counter();expired=0;previous={};changed=set();sample=[];repeated=set()
    max_error=0.
    for path in (folder/'fine_trace').glob('trace-*.jsonl'):
        worker=ready[int(path.stem.split('-')[1])].get('worker')
        for line in path.open():
            e=json.loads(line)
            if e['event']=='edf_priority_policy':
                assert e['policy']=='priority' and e['queue_type']=='PriorityRequestQueue'
                policies[int(worker)]=e
            elif e['event']=='edf_priority_received':
                rid=aliases[key(e['rid'])]
                assert rid not in received and e['metadata']==forwarded[rid],rid
                assert e['sort_deadline_s']==e['metadata']['deadline_s']
                assert e['priority']==round(e['metadata']['deadline_s']*1e9)
                received[rid]=dict(worker=int(worker),**e)
            elif e['event']=='edf_priority_refresh':
                counts['rounds']+=1
                now=e['now_monotonic_s'];by_queue={}
                for row in e['rows']:
                    rid=aliases[key(row['request_id'])];meta=forwarded[rid]
                    now_s,deadline,p=calculate(meta,now)
                    error=abs(meta['deadline_s']-row['sort_deadline_s']);max_error=max(max_error,error)
                    assert error<1e-12 and now_s==row['now_s'] and p==row['priority']==round(meta['deadline_s']*1e9),rid
                    assert row['deadline_s']==meta['deadline_s'] and row['T0_s']==meta['reference_service_s']
                    counts['request_updates']+=1
                    expired+=meta['deadline_s']<now_s
                    if rid in previous:
                        repeated.add(rid)
                        if previous[rid][1]!=row['priority']:changed.add(rid)
                    previous[rid]=(row['now_s'],row['priority'])
                    by_queue.setdefault(row['queue'],[]).append(row)
                for name,head in e['heads'].items():
                    rows=by_queue.get(name,[])
                    assert bool(rows)==bool(head)
                    if rows:
                        selected=next(r for r in rows if r['request_id']==head)
                        assert selected['priority']==min(r['priority'] for r in rows)
                        counts['native_heap_heads_checked']+=1
                if len(sample)<12:sample.append(dict(worker=worker,round=e['round'],now_monotonic_s=now,heads=e['heads'],rows=e['rows']))
    assert set(received)==set(records)
    config=json.loads((folder/'config.json').read_text())
    assert set(policies)==set(range(config['workers']))
    assert counts['rounds'] and counts['request_updates']
    assert not changed
    return dict(status='passed',requests=len(received),workers=sorted(policies),
        formula='deadline',refresh='each schedule, including running and skipped waiting',
        priority_unit='model nanosecond',**counts,expired_deadline_updates=expired,
        requests_refreshed_multiple_rounds=len(repeated),requests_with_changing_priority=len(changed),fixed_deadlines=True,max_formula_error_s=max_error,
        metadata_matches_arrived_profiles=True,samples=sample)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);args=p.parse_args()
    result=check(args.folder)
    (args.folder/'edf_priority.check.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='samples'}))
