"""Validate coverage/order of buffered native events before interpreting timings."""
import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path
import re


def key(value):
    if value is None:return None
    match=re.search(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',str(value))
    return match.group().replace('-','').lower() if match else None


def check(case):
    folder=Path(case)
    state=json.loads((folder/'admission.final.json').read_text())
    mapping={key(e['engine_uuid']):e['id'] for e in state['events'] if e['event']=='request_correlated'}
    rows=[]
    for p in (folder/'fine_trace').glob('trace-*.jsonl'):
        for s in p.read_text().splitlines():
            row=json.loads(s);row['pid']=int(p.stem.split('-')[1]);rows.append(row)
    assert rows
    byrequest=defaultdict(lambda:defaultdict(list))
    for r in rows:
        assert r['start_ns']<=r['end_ns']
        ids=([r['rid']] if r.get('rid') else [])+r.get('ids',[])+r.get('req_ids',[])
        for identity in set(ids):
            k=key(identity)
            if k in mapping:byrequest[mapping[k]][r['event']].append(r)
    assert set(byrequest)==set(state['records'])
    chain=[('worker_enter','start_ns'),('async_add_enter','start_ns'),
           ('ipc_encode','start_ns'),('ipc_encode','end_ns'),('ipc_send_ready','start_ns'),
           ('ipc_decode','start_ns'),('ipc_decode','end_ns'),
           ('core_preprocess','start_ns'),('core_preprocess','end_ns'),
           ('core_queue_put','start_ns'),('core_queue_get','start_ns'),
           ('scheduler_add','start_ns'),('scheduler_add','end_ns'),
           ('schedule','start_ns'),('schedule','end_ns'),('executor_enter','start_ns')]
    for source,events in byrequest.items():
        points=[]
        for event,field in chain:
            assert events[event],(source,event)
            points.append(min(x[field] for x in events[event]))
        assert all(a<=b for a,b in zip(points,points[1:])),(source,points)
        assert len(events['worker_enter'])==len(events['core_queue_put'])==1
        assert len(events['layer_compute_start'])==len(events['layer_compute_complete'])==8*len(events['executor'])
        assert len(events['worker_first_yield'])==1
        assert max(e['end_ns'] for e in events['executor'])<=events['worker_first_yield'][0]['start_ns']
    origins=[r for r in rows if r['event']=='gateway_origin'];assert len(origins)==1
    return dict(status='pass',requests=len(byrequest),events=len(rows),
        event_counts=dict(Counter(r['event'] for r in rows)),
        processes=len(list((folder/'fine_trace').glob('ready-*.json'))),
        gateway_origin_s=origins[0]['origin_s'],clock='same-host monotonic_ns',
        chain_points=len(chain),coverage='all requests',
        limits='Probe overhead is measured separately; nested spans are not additive.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('case');a=p.parse_args()
    result=check(a.case)
    (Path(a.case)/'fine_trace_check.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
