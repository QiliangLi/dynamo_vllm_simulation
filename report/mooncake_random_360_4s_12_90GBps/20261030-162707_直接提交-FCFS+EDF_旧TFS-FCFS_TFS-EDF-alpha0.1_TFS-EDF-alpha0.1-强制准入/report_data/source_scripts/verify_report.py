"""从逐请求表独立复核主表、账本、CDF和字节积分，再校验分享包。"""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import zipfile


def verify(root,finalize=False,visual_reviewed=False):
    root=Path(root);data=root/'report_data'
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    def rows(name):
        with (data/name).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    requests=rows('requests.csv');summary=rows('run_summary.csv');devices=rows('device_utilization.csv')
    cdf=rows('cdf.csv');band=rows('bandwidth_10ms.csv')
    inp=read(data/'input_check.json');expected=inp['total_read_bytes'];tolerance=expected*1e-11
    assert len(requests)==1800 and len(devices)==60 and len(summary)==5
    assert read(data/'matrix_receipt.json')['formal_requests']==1800
    result={}
    comparison={r['mode']:r for r in rows('bandwidth_comparison.csv')}
    prior_summary={r['mode']:r for r in rows('prior100/run_summary.csv')}
    prior_requests={(r['mode'],r['id']):r for r in rows('prior100/requests.csv')}
    prior_input=read(data/'prior100/input_check.json')
    assert prior_input['runtime_sources']==inp['runtime_sources']
    assert prior_input['native_sha256']==inp['native_sha256']
    for s in summary:
        mode=s['mode'];rr=[r for r in requests if r['mode']==mode]
        assert len(rr)==len({r['id'] for r in rr})==360
        assert Counter(r['class_id'] for r in rr)==Counter(A=90,B=270)
        assert sum(int(r['read_bytes']) for r in rr)==expected
        assert sum(int(r['u_tokens']) for r in rr)==207547
        values=sorted(float(r['gateway_ttft_ms']) for r in rr)
        assert math.isclose(statistics.mean(values),float(s['mean_ttft_ms']),abs_tol=1e-8)
        assert math.isclose(values[math.ceil(.95*len(values))-1],float(s['p95_ms']),abs_tol=1e-8)
        assert math.isclose(sum(int(r['slo_ok']) for r in rr)/3.6,float(s['slo_pct']),abs_tol=1e-8)
        assert math.isclose(360000/float(s['completion_ms']),float(s['requests_per_s']),abs_tol=1e-8)
        comp=comparison[mode];old=prior_summary[mode]
        assert float(comp['completion_100_ms'])==float(old['completion_ms'])
        assert float(comp['mean_100_ms'])==float(old['mean_ttft_ms'])
        assert math.isclose(float(comp['completion_change_pct']), (float(s['completion_ms'])/float(old['completion_ms'])-1)*100,abs_tol=1e-10)
        assert math.isclose(float(comp['mean_change_pct']), (float(s['mean_ttft_ms'])/float(old['mean_ttft_ms'])-1)*100,abs_tol=1e-10)
        fixed=sum(float(r['gateway_ttft_ms'])<=float(prior_requests[mode,r['id']]['slo_limit_ms']) for r in rr)/3.6
        assert math.isclose(float(comp['slo_90_on_100_deadlines_pct']),fixed,abs_tol=1e-10)
        assert all(int(r['slo_ok'])==int(float(r['gateway_ttft_ms'])<=float(r['slo_limit_ms'])) for r in rr)
        for group,n in [('all',360),('A',90),('B',270)]:
            cc=[r for r in cdf if r['mode']==mode and r['group']==group]
            chosen=[r for r in rr if group=='all' or r['class_id']==group]
            assert len(cc)==n and [int(r['rank']) for r in cc]==list(range(1,n+1))
            assert [float(r['ttft_ms']) for r in cc]==sorted(float(r['gateway_ttft_ms']) for r in chosen)
            assert all(math.isclose(float(r['cdf']),(i+1)/n,abs_tol=1e-12) for i,r in enumerate(cc))
            if group!='all':
                assert math.isclose(sum(int(r['slo_ok']) for r in chosen)/n*100,float(s[group+'_slo_pct']),abs_tol=1e-8)
        dd=[r for r in devices if r['mode']==mode]
        for d in dd:
            parts=[float(d[k]) for k in ['model_compute_s','io_without_model_compute_s','other_s']]
            assert min(parts)>=-1e-8
            assert math.isclose(sum(parts),float(d['completion_s']),abs_tol=1e-8)
        assert math.isclose(sum(float(d['model_compute_s']) for d in dd),float(s['model_compute_s']),abs_tol=1e-8)
        integral=math.fsum(float(r['actual_GBps'])*1e9*float(r['bin_width_ms'])/1000 for r in band if r['mode']==mode)
        assert abs(integral-expected)<tolerance,(mode,integral-expected)
        raw=data/'raw'/mode
        status=read(raw/'admission.final.json')
        formal={r['id']:status['records'][r['id']] for r in rr}
        span=max(r['first_token_s'] for r in formal.values())-min(r['profile']['arrival_s'] for r in formal.values())
        assert math.isclose(span*1000,float(s['completion_ms']),abs_tol=1e-7)
        assert read(raw/'online_feedback_check.json')['execution_feedback_rpcs']==0
        assert read(raw/'driver.json')['status']=='passed' and not read(raw/'driver.json')['alive']
        cfg=read(raw/'config.json')
        assert cfg['compute']['batch_time_model']=='glm5_direct_sum_v1'
        assert cfg['compute']['launch_saving_fraction']==1.0
        assert cfg['storage']['disk_Bps']==cfg['storage']['shared_link_Bps']==cfg['admission']['reference_bandwidth_Bps']==90e9
        assert (cfg['compute']['tp'],cfg['compute']['dp'],cfg['compute']['ep'])==(1,8,8)
        assert cfg['admission']['rpc_transport']=='dynamo'
        old_config=read(data/'prior100'/('config-'+mode+'.json'))
        old_config['storage'].update(disk_Bps=90e9,shared_link_Bps=90e9)
        old_config['admission']['reference_bandwidth_Bps']=90e9
        assert old_config==cfg
        singleton_s=0.;saved_us=0.;overhead_s=0.;step_count=0
        from json import loads
        bindings=read(raw/'storage.projection.json')['hit_bindings']
        for path in raw.glob('worker*.steps.jsonl'):
            for line in path.open():
                event=loads(line)
                timing=event.get('timing',{})
                if not event.get('num_scheduled_tokens') or event.get('prompt_phase')=='warmup':continue
                # The event IDs are vLLM UUIDs. Match shapes via the formal-only
                # read bindings, as the builder does; warmup remains in raw logs.
                ids={bindings[k.replace('-','')[:32]] for k in event['num_scheduled_tokens']}
                if not ids.intersection(formal):continue
                assert ids.issubset(formal)
                composition=timing['batch_composition']
                assert timing['batch_time_model']=='glm5_direct_sum_v1'
                assert composition['launch_saving_fraction']==1.0
                assert math.isclose(composition['saved_launch_us_total'],(len(ids)-1)*3*7*78,abs_tol=1e-9)
                singleton_s+=composition['sum_singleton_total_us']/1e6
                saved_us+=composition['saved_launch_us_total'];overhead_s+=timing['engine_overhead_s'];step_count+=1
        assert step_count==int(s['steps'])
        assert math.isclose(saved_us/1000,float(s['saved_launch_ms']),abs_tol=1e-8)
        assert math.isclose(singleton_s-saved_us/1e6+overhead_s,float(s['model_compute_s']),abs_tol=1e-9)
        if mode in ('edf','new-tfs','idle-bypass'):assert read(raw/'edf_priority.check.json')['fixed_deadlines']
        if mode in ('new-tfs','idle-bypass'):
            assert read(raw/'read_admission.check.json')['leaked_permits']==0
            assert read(raw/'routing.score.check.json')['status']=='passed'
        result[mode]=dict(formal=360,devices=12,slo_satisfied=sum(int(r['slo_ok']) for r in rr),
            completion_ms=span*1000,bandwidth_integral_error_bytes=integral-expected)
    report=next(root.glob('实验结果-*.md'));text=report.read_text(encoding='utf-8')
    assert [int(n) for n in re.findall(r'^## (\d+)\.',text,re.M)]==list(range(11))
    links=re.findall(r'\]\(([^)]+)\)',text)
    assert all((root/link).is_file() for link in links if '://' not in link and not link.endswith('validation.json'))
    from PIL import Image
    images=[]
    for p in sorted((root/'figure').glob('*.png')):
        with Image.open(p) as im:
            assert im.width>=1000 and im.height>=600
            images.append(dict(file=str(p.relative_to(root)),width=im.width,height=im.height))
    assert len(images)==7
    timeline_replacement=None
    replacement_path=data/'timeline_replacement.json'
    if replacement_path.is_file():
        timeline_replacement=read(replacement_path)
        assert timeline_replacement['status']=='passed' and timeline_replacement['simulations_run']==0
        assert timeline_replacement['image_sha256']==sha(root/'figure/02_full-sort-ms.png')
        assert timeline_replacement['timeline_validation_sha256']==sha(root/'online_timeline/validation.json')
        plot_validation=read(root/'online_timeline/validation.json')
        assert plot_validation['status']=='passed' and plot_validation['semantics']['historical_AB_used_for_classification'] is False
        assert timeline_replacement['published_time_accounts_equal']
        revision=read(data/'fixed_timeline_revision.json')
        for name,digest in revision['performance_files_sha256'].items():assert sha(data/name)==digest
        for name,digest in revision['unchanged_figure_sha256'].items():assert sha(root/'figure'/name)==digest
    receipt=dict(status='passed',formal_requests=1800,device_accounts=60,runs=result,
        same_runtime=read(data/'provenance.json')['same_runtime_fingerprints'],same_input=True,same_native=True,
        images=images,relative_links=len(links),bandwidth_integral_tolerance_bytes=tolerance,
        visual_reviewed=visual_reviewed,fixed_timeline=timeline_replacement)
    if finalize:
        assert visual_reviewed,'Inspect every new PNG first'
        receipt['files']={str(p.relative_to(root)):dict(bytes=p.stat().st_size,sha256=sha(p))
            for p in root.rglob('*') if p.is_file() and p.name!='validation.json' and '__pycache__' not in p.parts}
        (data/'validation.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        archive=root.parent/(root.name+'.zip')
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for p in sorted(root.rglob('*')):
                if p.is_file() and '__pycache__' not in p.parts:z.write(p,str(p.relative_to(root.parent)))
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            for info in z.infolist():
                assert hashlib.sha256(z.read(info.filename)).hexdigest()==sha(root.parent/info.filename)
        receipt=dict(status='passed',runs=result,zip_bytes=archive.stat().st_size,zip_sha256=sha(archive),visual_reviewed=True)
    return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--finalize',action='store_true');parser.add_argument('--visual-reviewed',action='store_true')
    args=parser.parse_args()
    print(json.dumps(verify(Path(__file__).resolve().parent,args.finalize,args.visual_reviewed),ensure_ascii=False))
