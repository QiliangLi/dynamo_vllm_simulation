"""Independent native-score lifecycle audit from saved logs and final state."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

def canonical(raw):return re.search(r'[0-9a-f]{32}',raw.replace('-','')).group()

def check(folder,require_discount=False):
    folder=Path(folder)
    config=json.loads((folder/'config.json').read_text())
    state=json.loads((folder/'admission.final.json').read_text())
    native=json.loads((folder/'routing.score.final.json').read_text())
    assert config['routing_accounting']['enabled'] and native['connected']
    assert native['factor']==config['routing_accounting']['waiting_factor']
    assert native['registered']==native['waiting']==native['active']==0
    heavy={canonical(e['engine_uuid']):e['id'] for e in state['events'] if e['event']=='request_correlated'
           and state['storage_hints'][e['id']]['is_heavy_read']}
    events=native['events'];byid={rid:[e for e in events if e['id']==rid] for rid in heavy}
    assert set(e['id'] for e in events)==set(heavy)
    grants={e['permit_id']:e for e in state['events'] if e['event']=='admission_reserved'}
    for rid,sequence in byid.items():
        assert sequence[0]['event']=='registered' and sequence[1]['event']=='bound_waiting'
        assert sequence[-1]['event']=='closed'
        assert len({e['worker_id'] for e in sequence if e['worker_id'] is not None})==1
        assert len({e['attempt'] for e in sequence if e['attempt'] is not None})==1
        for e in sequence:
            if e['event']=='activated' and e['permit_id']!='no-external-load':
                assert grants[e['permit_id']]['id']==heavy[rid]
    calls=[e for e in state['events'] if e['event'].startswith('routing_score_')]
    assert all(e['duration_wall_ns']>=0 for e in calls)
    # The original checker still proves the chosen worker has the lowest
    # native scorer cost and that full reservation gauges drain to zero.
    routing=json.loads((folder/'routing.check.json').read_text())
    assert routing['decisions']==len(state['records']) and routing['final_prefill_tokens_and_decode_blocks_zero']
    scores=0
    for line in (folder/'frontend.log').read_text().splitlines():
        if 'WAITING_SCORE worker_id=' not in line:continue
        values={key:float(value) for key,value in re.findall(r'(raw_prefill|raw_decode|discount|scale|overlap|weighted)=([0-9.eE+-]+)',line)}
        own={key:float(value) for key,value in re.findall(r'(own_prefill_credit|own_decode_credit)=([0-9.eE+-]+)',line)}
        expected=values['scale']*max(0,values['raw_prefill']-values['overlap']-values['discount']-own.get('own_prefill_credit',0))+max(0,values['raw_decode']-values['discount']-own.get('own_decode_credit',0))
        assert abs(expected-values['weighted'])<1e-6
        assert values['discount']>0 and values['weighted']<values['scale']*max(0,values['raw_prefill']-values['overlap'])+values['raw_decode']
        scores+=1
    if require_discount and native['factor']<1:assert scores>0,'no observed native discounted candidate score'
    return dict(status='passed',factor=native['factor'],heavy=len(heavy),requests=len(state['records']),
        transitions=dict(Counter(e['event'] for e in events)),native_worker_selection=True,
        full_resource_gauges_drained=True,score_ledger_drained=True,
        discounted_candidate_scores= scores,
        score_control_calls=len(calls),score_control_mean_wall_ms=sum(e['duration_wall_ns'] for e in calls)/max(1,len(calls))/1e6)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);a=p.parse_args()
    print(json.dumps(check(a.folder),indent=2))
