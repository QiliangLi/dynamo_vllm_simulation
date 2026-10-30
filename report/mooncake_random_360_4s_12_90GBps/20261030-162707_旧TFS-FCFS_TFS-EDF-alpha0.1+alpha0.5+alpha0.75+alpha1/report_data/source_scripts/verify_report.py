"""Independent checks for the alpha experiment report and its ZIP."""
import argparse
from bisect import bisect_right
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import zipfile


def verify(root, finalize=False, visual_reviewed=False):
    root=Path(root);data=root/'report_data'
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    def rows(name):
        with (data/name).open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))
    requests,summary,devices,cdf,band=[rows(name) for name in ['requests.csv','run_summary.csv','device_utilization.csv','cdf.csv','bandwidth_10ms.csv']]
    assert len(requests)==1800 and len(devices)==60 and len(summary)==5
    receipt=read(data/'matrix_receipt.json');assert receipt['new_formal_requests']==1080 and receipt['reused_formal_requests']==720
    tolerance=read(data/'input_check.json')['total_read_bytes']*1e-11
    provenance=read(data/'provenance.json');assert provenance['only_alpha_differs_between_new_tfs']
    base=read(data/'raw/a01/config.json')
    result={}
    for run in summary:
        mode=run['mode'];rr=[r for r in requests if r['mode']==mode];dd=[r for r in devices if r['mode']==mode]
        assert len(rr)==len({r['id'] for r in rr})==360 and Counter(r['class_id'] for r in rr)==Counter(A=90,B=270)
        assert sum(int(r['read_bytes']) for r in rr)==539258683392
        assert sum(int(r['u_tokens']) for r in rr)==207547
        values=sorted(float(r['gateway_ttft_ms']) for r in rr)
        assert math.isclose(statistics.mean(values),float(run['mean_ttft_ms']),abs_tol=1e-8)
        assert math.isclose(values[math.ceil(.95*len(values))-1],float(run['p95_ms']),abs_tol=1e-8)
        assert math.isclose(sum(int(r['slo_ok']) for r in rr)/3.6,float(run['slo_pct']),abs_tol=1e-8)
        assert all(int(r['slo_ok'])==int(float(r['gateway_ttft_ms'])<=float(r['slo_limit_ms'])) for r in rr)
        for group,n in [('all',360),('A',90),('B',270)]:
            cc=[r for r in cdf if r['mode']==mode and r['group']==group]
            chosen=[r for r in rr if group=='all' or r['class_id']==group]
            assert len(cc)==n and [int(r['rank']) for r in cc]==list(range(1,n+1))
            assert [float(r['ttft_ms']) for r in cc]==sorted(float(r['gateway_ttft_ms']) for r in chosen)
            assert all(math.isclose(float(r['cdf']),(i+1)/n,abs_tol=1e-12) for i,r in enumerate(cc))
        for d in dd:
            parts=[float(d[k]) for k in ['model_compute_s','io_without_model_compute_s','other_s']]
            assert min(parts)>=-1e-8 and math.isclose(sum(parts),float(d['completion_s']),abs_tol=1e-8)
        assert math.isclose(sum(float(d['model_compute_s']) for d in dd),float(run['model_compute_s']),abs_tol=1e-8)
        assert math.isclose(sum(float(d['io_without_model_compute_s']) for d in dd),float(run['red_instance_s']),abs_tol=1e-8)
        integral=math.fsum(float(r['actual_GBps'])*1e9*float(r['bin_width_ms'])/1000 for r in band if r['mode']==mode)
        assert abs(integral-539258683392)<tolerance,(mode,integral)
        raw=data/'raw'/mode;state=read(raw/'admission.final.json');formal={r['id']:state['records'][r['id']] for r in rr}
        span=max(r['first_token_s'] for r in formal.values())-min(r['profile']['arrival_s'] for r in formal.values())
        assert math.isclose(span*1000,float(run['completion_ms']),abs_tol=1e-7)
        assert read(raw/'online_feedback_check.json')['execution_feedback_rpcs']==0
        driver=read(raw/'driver.json');assert driver['status']=='passed' and not driver['alive'] and not driver['guards']
        assert driver['max_swap_gib']==0
        assert read(raw/'warmup.check.json')['coverage']==list(range(12))
        assert len(read(raw/'warmup.history.check.json')['excluded_history_ids'])==72
        assert read(raw/'formula_check.json')['max_abs_error_s']<1e-8
        assert read(raw/'routing.build.json')['native_sha256']==provenance['native_sha256']
        origin=read(raw/'clock.json')['gateway_origin_monotonic_s']
        start=origin+min(r['profile']['arrival_s'] for r in formal.values());end=start+span
        gc=read(raw/'storage.gc.end.json');begin=read(raw/'storage.gc.formal.begin.json')
        assert gc['policy']=='defer_during_run' and not gc['active'] and gc['enabled'] and not gc['failure']
        assert begin['active'] and not begin['enabled'] and begin['events'][-1]['t']<start
        assert gc['events'][-1]['t']>end and not [e for e in gc['events'] if start<=e['t']<=end]
        assert all(r['state']=='FINISHED' and not r['lease_open'] for r in formal.values())
        cfg=read(raw/'config.json');assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==cfg['admission']['reference_bandwidth_Bps']==90e9
        assert cfg['compute']['batch_time_model']=='glm5_direct_sum_v1' and cfg['compute']['launch_saving_fraction']==1
        if mode!='old-tfs':
            alpha={'a01':.1,'a05':.5,'a075':.75,'a1':1.}[mode]
            expected=json.loads(json.dumps(base));expected['routing_accounting']['waiting_factor']=alpha
            assert cfg==expected
            assert read(raw/'routing.score.check.json')['factor']==alpha
            assert read(raw/'read_admission.check.json')['leaked_permits']==0
            assert read(raw/'edf_priority.check.json')['fixed_deadlines']
        singleton=saved=overhead=0.;count=0;bindings=read(raw/'storage.projection.json')['hit_bindings']
        for path in raw.glob('worker*.steps.jsonl'):
            for line in path.open():
                event=json.loads(line)
                if not event.get('num_scheduled_tokens'):continue
                ids={bindings[k.replace('-','')[:32]] for k in event['num_scheduled_tokens']}
                if not ids.intersection(formal):continue
                assert ids.issubset(formal)
                timing=event['timing'];c=timing['batch_composition']
                assert timing['batch_time_model']=='glm5_direct_sum_v1'
                assert math.isclose(c['saved_launch_us_total'],(len(ids)-1)*3*7*78,abs_tol=1e-9)
                singleton+=c['sum_singleton_total_us']/1e6;saved+=c['saved_launch_us_total']/1e6;overhead+=timing['engine_overhead_s'];count+=1
        assert count==int(run['steps']) and math.isclose(singleton-saved+overhead,float(run['model_compute_s']),abs_tol=1e-9)
        result[mode]=dict(formal=360,slo_satisfied=sum(int(r['slo_ok']) for r in rr),completion_ms=span*1000,bandwidth_error_B=integral-539258683392)
    workspace=next(p for p in root.parents if (p/'workspace.json').is_file())
    prior=workspace/'note/tfs_dv/Mooncake360-90GB直接组合五方案-20261029/report_data'
    with (prior/'run_summary.csv').open() as stream:original={r['mode']:r for r in csv.DictReader(stream)}
    for mode,source in [('old-tfs','old-tfs'),('a01','new-tfs')]:
        current=next(r for r in summary if r['mode']==mode)
        for key in ['completion_ms','mean_ttft_ms','p95_ms','slo_pct','model_compute_s','mean_batch_size']:
            assert math.isclose(float(current[key]),float(original[source][key]),abs_tol=1e-8)
    classification_checks={}
    layer_rows=rows('online_classification_layers.csv')
    request_classes=rows('online_classification_requests.csv')
    class_summary={r['mode']:r for r in rows('online_classification_summary.csv')}
    assert len(request_classes)==1800
    for run in summary:
        mode=run['mode'];status=read(data/'raw'/mode/'admission.final.json')
        records={rid:r for rid,r in status['records'].items() if rid.startswith('conversation:')}
        origin=read(data/'raw'/mode/'clock.json')['gateway_origin_monotonic_s']
        first=min(r['profile']['arrival_s'] for r in records.values())
        thresholds=[e for e in status['events'] if e['event']=='threshold_updated'];times=[e['t'] for e in thresholds]
        seen={rid:set() for rid in records};ll=[r for r in layer_rows if r['mode']==mode]
        assert len(ll)==int(run['steps'])*78
        for layer in ll:
            when=first+float(layer['start_s']);index=bisect_right(times,when)-1
            assert index>=0
            threshold=thresholds[index]
            assert int(layer['threshold_version'])==threshold['threshold_version']
            ids=json.loads(layer['request_ids']);expected=[]
            for rid in ids:
                if mode=='old-tfs':
                    p=records[rid]['profile']['pressure_Bps'];theta=threshold['theta']
                    heavy=theta is not None and p>0 and p>=theta
                else:heavy=status['storage_hints'][rid]['is_heavy_read']
                value='heavy' if heavy else 'light';expected.append(value);seen[rid].add(value)
            assert int(layer['heavy_requests'])==expected.count('heavy')
            assert int(layer['light_requests'])==expected.count('light')
            expected_state=expected[0] if len(set(expected))==1 else 'mixed'
            assert layer['state']==expected_state
        assert math.isclose(sum(float(r['model_s']) for r in ll),float(run['model_compute_s']),abs_tol=1e-8)
        q=[r for r in request_classes if r['mode']==mode]
        assert len(q)==360
        for row in q:
            assert set(row['compute_classes'].split('/'))==seen[row['id']]
            if mode!='old-tfs':
                assert row['admission_class']==('heavy' if status['storage_hints'][row['id']]['is_heavy_read'] else 'light')
                assert len(seen[row['id']])==1
        cs=class_summary[mode]
        assert int(cs['compute_heavy_any'])==sum('heavy' in value for value in seen.values())
        assert int(cs['compute_light_any'])==sum('light' in value for value in seen.values())
        assert int(cs['changed_requests'])==sum(len(value)>1 for value in seen.values())
        if mode=='a075':
            for rid in ('conversation:L222','conversation:L208','conversation:L269','conversation:L97','conversation:L273','conversation:L309'):
                assert seen[rid]=={'heavy'}
        classification_checks[mode]=dict(layers=len(ll),requests=360,changed_requests=int(cs['changed_requests']))
    revision_path=data/'classification_revision.json'
    if revision_path.is_file():
        for name,digest in read(revision_path)['baseline_performance_sha256'].items():
            assert sha(data/name)==digest,('Performance file changed during recolor',name)
    report=next(root.glob('实验结果-*.md'));text=report.read_text(encoding='utf-8')
    assert [int(n) for n in re.findall(r'^## (\d+)\.',text,re.M)]==list(range(11))
    links=re.findall(r'\]\(([^)]+)\)',text)
    assert all((root/link).is_file() for link in links if '://' not in link and not link.endswith('validation.json'))
    from PIL import Image
    images=[]
    for path in sorted((root/'figure').glob('*.png')):
        with Image.open(path) as im:
            assert im.width>=1000 and im.height>=600
            images.append(dict(file=str(path.relative_to(root)),width=im.width,height=im.height))
    assert len(images)==7
    checked=dict(status='passed',formal_requests=1800,new_formal_requests=1080,reused_formal_requests=720,
        device_accounts=60,runs=result,same_runtime=True,same_native=True,only_alpha_config_diff=True,
        online_classification_checked=classification_checks,performance_unchanged_after_recolor=revision_path.is_file(),
        images=images,relative_links=len(links),bandwidth_integral_tolerance_bytes=tolerance,visual_reviewed=visual_reviewed)
    if finalize:
        assert visual_reviewed
        checked['files']={str(p.relative_to(root)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in root.rglob('*') if p.is_file() and p.name!='validation.json' and '__pycache__' not in p.parts}
        (data/'validation.json').write_text(json.dumps(checked,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        archive=root.parent/(root.name+'.zip')
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for path in sorted(root.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:z.write(path,str(path.relative_to(root.parent)))
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            for info in z.infolist():assert hashlib.sha256(z.read(info.filename)).hexdigest()==sha(root.parent/info.filename)
        checked=dict(status='passed',runs=result,zip_bytes=archive.stat().st_size,zip_sha256=sha(archive),visual_reviewed=True)
    return checked


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--finalize',action='store_true');parser.add_argument('--visual-reviewed',action='store_true');args=parser.parse_args()
    print(json.dumps(verify(Path(__file__).resolve().parent,args.finalize,args.visual_reviewed),ensure_ascii=False))
