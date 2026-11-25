import json
import numpy as np
from sim.grouped_storage import GroupedStorage
bin_s = .05
def read_chart(path, origin, wall):
    get = lambda name: json.loads((path / name).read_text())
    report, storage, config = get('report.json'), get('storage.final.json'), get('config.json')
    reqs = report['requests'].values()
    edges = np.r_[np.arange(0, wall, bin_s), wall]
    widths = np.diff(edges)
    storage_origin = storage['t0_monotonic_s']
    assert config['wall_time_scale'] == 1
    store = GroupedStorage(config['storage'], config['block_size'])
    history = sorted(storage['layer_read_history'], key=lambda r:r['submitted_s'])
    loads = storage['layer_loads']
    recorded = {(r['rid'], r['layer']):r['completed_s'] for r in history}
    checked = {}
    actions = [(r['submitted_s'], i, r) for i,r in enumerate(history)]
    actions += [(origin-storage_origin+t,-1,None) for t in edges]
    cumulative = []
    for time, _, row in sorted(actions):
        while store.now < time:
            target = min(time, store.next_time())
            for key in store.advance(target):
                checked[key] = target
        if row is None:
            cumulative.append(store.bytes_transferred)
        else:
            rid = row['rid']
            load = loads[rid]
            store.submit_counted(time, (rid,row['layer']), {int(i):v for i,v in load['counts'].items()},
                                 storage['layer_spec']['bytes_per_token']*config['block_size'],
                                 load['demand'], row['read_latency_s'])
    assert len(checked)==len(recorded)==len(history)
    err = max(abs(checked[k]-t) for k,t in recorded.items())
    assert err < 1e-6,err
    achieved = np.diff(cumulative) / widths / 1e9
    assert max(achieved)<25.0001 and abs(sum(np.diff(cumulative))-storage['bytes_transferred'])<1
    demand_vol = np.zeros_like(widths)
    first = {r['rid']: storage_origin + r['submitted_s'] for r in history if r['layer']==0}
    count = 0
    for file in sorted(path.glob('worker*.steps.jsonl')):
        starts, ends, times = {}, {}, {}
        for line in file.open():
            event = json.loads(line)
            kind = event.get('event')
            if kind in ('layer_compute_start','layer_compute_complete'):
                rid, = event['req_ids']
                if kind=='layer_compute_start' and event['layer']==0: starts.setdefault(rid,event['occurred_mono'])
                if kind=='layer_compute_complete' and event['layer']==15: ends[rid]=event['occurred_mono']
            elif kind is None and 'timing' in event:
                rid, = event['req_ids']
                pass # A request can span several prefill steps
                times[rid]=times.get(rid,0.)+event['timing']['duration_s']
        assert starts.keys()==ends.keys()==times.keys()
        count+=len(times)
        for rid,cost in times.items():
            if rid not in loads: continue
            start = min(first[rid],starts[rid])-origin
            end = ends[rid]-origin
            assert 0<=start<end<=wall
            demand_vol += loads[rid]['total_bytes']/1e9/cost * np.maximum(0,np.minimum(edges[1:],end)-np.maximum(edges[:-1],start))
    assert count==len(report["requests"])
    demand = demand_vol/widths
    return edges, demand, achieved, dict(over_s=float(widths[demand>25].sum()),wall_s=wall,
        read_GB=storage['bytes_transferred']/1e9, achieved_avg=storage['bytes_transferred']/1e9/wall,max_replay_error_us=err*1e6)

