"""独立核对紧凑报告数据，生成文件指纹与通过CRC检查的分享包。"""
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import tarfile
import zipfile

HERE=Path(__file__).resolve().parent
W=next((p for p in HERE.parents if (p/'workspace.json').is_file()),None)
OUT=HERE if (HERE/'report_data').is_dir() else W/'note/tfs_dv/Mooncake全局4%-90GB三方案-20261029'
D=OUT/'report_data'
ARMS=('direct','tfs-a01','tfs-a1')
EXPECTED=539571462144
read=lambda p:json.loads(p.read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def csvread(path):
    with path.open() as f:return list(csv.DictReader(f))


def near(a,b,abs_tol=1e-7):
    assert math.isclose(float(a),float(b),rel_tol=1e-10,abs_tol=abs_tol),(a,b)


def freeze_sources():
    """保存小型源码与审计，不复制原始事件或环境。"""
    if W is None or HERE==OUT:return
    exp=W/'data/dynamo_vllm_simulation/dynamo_vllm_cpu_sim/results/mooncake360_global4_90g_20261029'
    rep=OUT/'reproduce';rep.mkdir(exist_ok=True)
    provenance=read(D/'provenance.json')
    assert provenance['ABC']['runtime_sha256']==provenance['ABCD']['runtime_sha256']
    root=exp/'ABC/package'
    with tarfile.open(rep/'runtime_source.tar.gz','w:gz') as archive:
        for name,expected in provenance['ABC']['runtime_sha256'].items():
            assert sha(root/name)==expected,name
            archive.add(root/name,arcname=name,recursive=False)
        for name in ('versions.json','source-fingerprints.json'):
            archive.add(root/name,arcname=name,recursive=False)
    for dataset in ('ABC','ABCD'):
        case=rep/dataset;case.mkdir(exist_ok=True)
        for name in ('base_runner.py','controlled_gc_gateway_20261027.py','tfs_warmup_gateway_20261027.py','source_worktree.diff'):
            shutil.copy2(exp/dataset/name,case/name)
        shutil.copy2(W/f'temp/tfs_opt_global4_20261029/{dataset}/allocation.csv',case/'allocation.csv')
        shutil.copy2(exp/dataset/'package/results/input360/config.json',case/'config.json')
        for arm in ARMS:
            shutil.copy2(exp/dataset/f'executed-{arm}.py',case/f'executed-{arm}.py')
            audit=case/arm;audit.mkdir(exist_ok=True)
            source=exp/dataset/f'package/results/{arm}/run'
            for name in ('global4.check.json','check.json','formula_check.json','layer_check.json','hit_check.json','warmup.check.json'):
                shutil.copy2(source/name,audit/name)
    for name in ('analyze.py','run.py','extract.py','README.md'):
        shutil.copy2(HERE/name,rep/name if name!='README.md' else rep/'experiment_README.md')
    shutil.copy2(__file__,OUT/'verify_report.py')
    (rep/'README.md').write_text('''# 复现范围

## 重画与验证报告

在报告目录运行 `python build_report.py`，需要 matplotlib、numpy 和可用中文字体。
运行 `python verify_report.py` 核对小表、CDF、时间账本、带宽积分、源码指纹与相对链接，
并重建报告目录旁的ZIP。报告目录内的数据足够完成这些操作，不依赖原始实验目录。

## 复跑仿真

runtime_source.tar.gz保存本次执行的sim/scripts/configs/patched和适配器文件。
它是DV未提交工作树的精确小型快照，含新增trace_io.py；不能仅用source_head替代。
ABC/ABCD含冻结驱动、配置、实际执行入口、分配表和小型验收记录。
原始Mooncake的output_length和hash_ids未参与本次构造；人工缓存量不是实测命中。

完整重跑仍需要原工作区布局、匹配versions/source-fingerprints的上游、Python虚拟环境，
以及provenance.json指定SHA的加权block16 Dynamo原生构建。分享包不含这些大依赖，
因此不是解压后即可独立启动的完整运行环境。run.py中路径按工作区解释。
prepare拒绝覆盖已有实验；正式记录不可覆盖。若移植到新环境，需先单独完成依赖验收，
再修改本地实验输出路径并保留新旧指纹。不能把不同环境的新轮次混入本次结果。

本轮配置：单共享池90e9 B/s、K=1、12实例、每步最多4请求/8192新token，
78层流水、TP1/DP8/EP8、每方案72预热+360正式、各一次。
完整原始事件保留本机DV results/mooncake360_global4_90g_20261029，未放入ZIP。
''')


def main():
    freeze_sources()
    summary=read(D/'summary.json');requests=csvread(D/'requests.csv')
    devices=csvread(D/'device_utilization.csv');cdf=csvread(D/'cdf.csv');bands=csvread(D/'bandwidth_10ms.csv')
    profiles=read(D/'input_profiles.json');provenance=read(D/'provenance.json')
    with gzip.open(D/'trajectories.json.gz','rt') as f:traces=json.load(f)
    assert len(summary)==6 and len(requests)==2160 and len(devices)==72
    errors=[]
    for dataset in ('ABC','ABCD'):
        intended={r['id']:r for r in profiles[dataset]}
        assert len(intended)==360 and sum(r['u'] for r in intended.values())==204699
        assert sum(r['prompt_tokens'] for r in intended.values())==5117755
        assert sum(r['read_bytes'] for r in intended.values())==EXPECTED
        allocation=csvread(OUT/f'reproduce/{dataset}/allocation.csv')
        assert len(allocation)==360
        assert sha(OUT/f'reproduce/{dataset}/allocation.csv')==provenance[dataset]['allocation_sha256']
        assert sha(OUT/f'reproduce/{dataset}/config.json')==provenance[dataset]['config_sha256']
        assert all(int(r['h'])%16==0 and int(r['n'])==int(r['h'])+int(r['u']) for r in allocation)
        for arm in ARMS:
            s=next(r for r in summary if (r['dataset'],r['arm'])==(dataset,arm))
            rr=[r for r in requests if (r['dataset'],r['arm'])==(dataset,arm)]
            assert len(rr)==360 and len({r['id'] for r in rr})==360
            assert sum(int(r['new_tokens']) for r in rr)==204699
            assert sum(int(r['read_bytes']) for r in rr)==EXPECTED
            ttft=sorted(float(r['ttft_ms']) for r in rr)
            near(statistics.mean(ttft),s['mean_ttft_ms']);near(ttft[341],s['p95_ms'])
            for r in rr:
                p=intended[r['id']]
                assert (int(r['input_tokens']),int(r['remote_tokens']),int(r['new_tokens']),int(r['read_bytes']))==(p['prompt_tokens'],p['h'],p['u'],p['read_bytes'])
                near(r['deadline_ms'],p['deadline_width_s']*1000)
                assert int(r['slo'])==int(float(r['ttft_ms'])<=float(r['deadline_ms']))
            near(sum(int(r['slo']) for r in rr)/3.6,s['slo_pct'])
            for label in ('all',*dataset):
                group=rr if label=='all' else [r for r in rr if r['class_id']==label]
                cc=[r for r in cdf if (r['dataset'],r['arm'],r['class_id'])==(dataset,arm,label)]
                assert len(cc)==len(group)
                assert [int(r['rank']) for r in cc]==list(range(1,len(group)+1))
                assert [float(r['ttft_ms']) for r in cc]==sorted(float(r['ttft_ms']) for r in group)
                for i,r in enumerate(cc,1):near(r['cdf'],i/len(group))
                assert float(cc[-1]['cdf'])==1
            dd=[r for r in devices if (r['dataset'],r['arm'])==(dataset,arm)]
            for d in dd:
                near(sum(float(d[k]) for k in ('model_s','io_without_model_s','other_s')),d['span_s'])
                near(float(d['model_s'])/float(d['span_s'])*100,d['model_pct'])
                near(float(d['span_s'])*1000,s['completion_ms'])
            near(sum(float(d['model_s']) for d in dd),s['model_s'])
            tr=next(r for r in traces if (r['dataset'],r['arm'])==(dataset,arm))
            integral=math.fsum((b-a)*rate*1e9 for a,b,_,rate in tr['band'])
            bucket_integral=math.fsum(float(r['actual_GBps'])*1e7 for r in bands if (r['dataset'],r['arm'])==(dataset,arm))
            assert abs(integral-EXPECTED)<6 and abs(bucket_integral-EXPECTED)<6
            errors.append(dict(dataset=dataset,arm=arm,interval_error_B=integral-EXPECTED,bucket_error_B=bucket_integral-EXPECTED))
    with tarfile.open(OUT/'reproduce/runtime_source.tar.gz') as archive:
        for name,expected in provenance['ABC']['runtime_sha256'].items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest()==expected
    figures=sorted((OUT/'figure').glob('*.png'));assert len(figures)==12
    from PIL import Image
    for p in figures:
        with Image.open(p) as im:
            assert im.width>=1500 and im.height>=600
            im.verify()
    links=[]
    for md in OUT.rglob('*.md'):
        for target in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)',md.read_text()):
            if '://' in target or target.startswith('#'):continue
            p=(md.parent/target.split('#')[0]).resolve()
            # validation is generated just below.
            assert p.is_file() or p==D/'validation.json',(md,target)
            links.append(str(p.relative_to(OUT)))
    validation=dict(status='passed',formal_requests=2160,warmup_requests=432,runs=6,
        K=1,bandwidth_Bps=90e9,cdf_groups=27,time_ledgers=72,figures=12,
        checks=['profile/input/deadline identity','mean/p95/SLO independently recomputed',
                'CDF sorted ranks/mass/endpoints','model+IO+other time ledgers',
                'bandwidth interval and 10ms bucket byte integrals','runtime archive SHA256',
                'PNG decode and dimensions','all relative Markdown links'],
        byte_tolerance_B=6,byte_errors=errors,relative_links=len(links),
        visual_review=dict(status='passed',figures=[p.name for p in figures],
            method='逐张打开检查中文、颜色、图例、坐标、标注和留白；带宽三图共用对数纵轴。'),
        limitations=['single repeat per arm','battery power and pre-existing host swap',
                     'synthetic cache allocation, prefill-only, formula not calibrated on Ascend910C'],
        sha256={str(p.relative_to(OUT)):sha(p) for p in sorted(OUT.rglob('*'))
                if p.is_file() and p.name!='validation.json' and '__pycache__' not in p.parts})
    (D/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2)+'\n')
    target=OUT.parent/(OUT.name+'.zip')
    files=[p for p in OUT.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for p in sorted(files):archive.write(p,str(Path(OUT.name)/p.relative_to(OUT)))
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None and len(archive.namelist())==len(files)
        for p in files:assert archive.read(str(Path(OUT.name)/p.relative_to(OUT)))==p.read_bytes()
    print(json.dumps(dict(status='passed',formal=2160,cdf_groups=27,time_ledgers=72,
        figures=12,zip_bytes=target.stat().st_size,zip_sha256=sha(target)),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
