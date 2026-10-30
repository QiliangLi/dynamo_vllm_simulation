"""报告目录生成、归档校验和标准数据重绘；不运行仿真。"""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
from urllib.parse import unquote
from datetime import datetime

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent
read=lambda p:json.loads(p.read_text(encoding='utf-8'))

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def entries():
    return [(p.parent,read(p)) for p in sorted(ROOT.glob('*/*/manifest.json'))]

def report_path(value):
    target=(ROOT/value).resolve()
    if not target.is_relative_to(ROOT.resolve()) or not (target/'manifest.json').is_file():
        raise ValueError('Report path must identify an existing report folder')
    return target

def verify_one(folder,m,writing=False):
    assert folder.relative_to(ROOT).as_posix()==m['report_id']
    assert folder.parent.name==m['benchmark_id']
    created=datetime.fromisoformat(m['created_at'])
    assert created.tzinfo is not None and folder.name.startswith(created.strftime('%Y%m%d-%H%M%S')+'_')
    bm=read(REPO/'benchmark'/m['benchmark_id']/'manifest.json')
    assert m['benchmark_input_sha256']==bm['original_trace_sha256']
    assert m['simulations_run']==0 and m['performance_recomputed'] is False
    for rel,expected in m['files'].items():
        target=(folder/rel).resolve()
        assert target.is_relative_to(folder.resolve()) and target.is_file(),rel
        assert target.stat().st_size==expected['bytes'] and digest(target)==expected['sha256'],rel
    for image in m['figures']:
        target=folder/image['path'];raw=target.read_bytes()
        assert raw[:8]==b'\x89PNG\r\n\x1a\n' and digest(target)==image['source_sha256']
        assert struct.unpack('>II',raw[16:24])==(image['width'],image['height'])
    # Every copied table retains the exact source bytes, including compressed large tables.
    tables=0
    for rel,source in m['source_files'].items():
        if not rel.endswith('.csv'):continue
        target=folder/rel
        if target.exists():actual=digest(target)
        elif (folder/(rel+'.gz')).exists():
            h=hashlib.sha256()
            with gzip.open(folder/(rel+'.gz'),'rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
            actual=h.hexdigest()
        else:raise AssertionError('Missing source table: '+rel)
        assert actual==source['sha256'],rel
        tables+=1
    # Archived Markdown uses only working relative links or explicit external links.
    for path in folder.rglob('*.md'):
        if 'source_scripts' in path.parts:continue
        for value in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
            if '://' in value or value.startswith('#'):continue
            target=(path.parent/unquote(value.split('#')[0])).resolve()
            if writing and target==(folder/'archive_validation.json').resolve():continue
            assert target.is_relative_to(REPO.resolve()) and target.exists(),(str(path),value)
    source_check=folder/'report_data/source_validation.json'
    status=read(source_check).get('status') if source_check.exists() else None
    return dict(status='passed',scope='archive integrity, source tables and image identity',
                source_validation_status=status,source_visual_reviewed=m.get('source_visual_reviewed'),
                file_count=len(m['files']),figures=len(m['figures']),source_csv_tables=tables,
                simulations_run=0,performance_recomputed=False)

def verify(write=False):
    results=[]
    for folder,m in entries():
        result=verify_one(folder,m,writing=write)
        if write:(folder/'archive_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        results.append(dict(report_id=m['report_id'],study_id=m['study_id'],**result))
    assert results,'No reports'
    return dict(status='passed',report_entries=len(results),unique_source_studies=len({r['study_id'] for r in results}),
                benchmarks=len({m['benchmark_id'] for _,m in entries()}),reports=results,simulations_run=0)

def catalog_text():
    labels={'matched_input_and_model':'负载/模型对应；保留原运行版本',
            'historical_model_and_feedback':'历史模型/反馈版本',
            'research_runtime_export_only':'研究版结果；交付运行器export_only'}
    lines=['# 报告目录','','每行是一处归档入口；study_id相同表示同一研究，不重复计为独立实验。目录时间是归档创建时间，实际实验日期另列。','',
           '|Benchmark|报告创建时间（北京时间）|实际实验日期|实际比较方案|版本关系|报告|',
           '|---|---|---|---|---|---|---|']
    for _,m in entries():
        stamp=datetime.fromisoformat(m['created_at']).strftime('%Y-%m-%d %H:%M:%S')
        link=m['report_id']+'/report.md'
        lines.append(f"| {m['benchmark_id']} | {stamp} | {'、'.join(m['experiment_dates'])} | {'；'.join(s['label'] for s in m['schemes'])} | {labels[m['compatibility']]} | [查看]({link}) |")
    studies=len({m['study_id'] for _,m in entries()})
    lines+=['',f'共 {len(entries())} 个报告入口，来自 {studies} 份源研究报告。','',
        '## 附录：归档口径','',
        '- 90 GB/s的全局ABC与ABCD来源于同一联合报告，分别在两个benchmark目录保留完整入口；两处study_id相同，不是两次复跑。',
        '- 两份random历史报告使用direct_sum和关闭执行反馈；当前同名benchmark使用actual_batch_work_v2和同步batch反馈，不能直接把历史指标当作当前配置结果。',
        '- AgentX与synthetic报告来自独立研究版运行，交付运行器的export_only状态保持。',
        '- 不纳入100 GB/s的两组独立alpha0.5补充图、附表及对应旧参考小节。random历史alpha扫描中的alpha0.5属于已选报告，仍按原实验保留。',
        '- 图像保持原始字节；较大派生JSON/CSV使用gzip无损压缩。大型原始事件不随本目录发布；来源和哈希在manifest与source_validation/provenance中。',
        '- source_validation.json是原报告验证记录；archive_validation.json只验证本次归档，不表示重新运行了实验。',
        '- 新报告增加独立目录和manifest后，运行python report/manage.py verify --write与catalog更新索引；通用README和agent.md不维护逐报告名单。','']
    return '\n'.join(lines)

def redraw(path,output):
    folder=report_path(path);m=read(folder/'manifest.json');verify_one(folder,m)
    dataset=m.get('standard_dataset')
    if not dataset:raise SystemExit('该历史报告没有当前标准dataset；可查看配图/复核表格，不承诺通用重绘。见本报告reproduce/README.md。')
    output=Path(output).resolve()
    if output.exists():raise SystemExit('输出目录已存在；请指定新目录。')
    with tempfile.TemporaryDirectory(prefix='report-redraw-') as temp:
        target=Path(temp)/'dataset.json';source=folder/dataset
        if source.suffix=='.gz':
            import shutil
            with gzip.open(source,'rb') as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
        else:target.write_bytes(source.read_bytes())
        subprocess.run([sys.executable,str(ROOT/'tools/generate_report.py'),'--dataset',str(target),'--output',str(output)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='action',required=True)
    s.add_parser('list')
    v=s.add_parser('verify');v.add_argument('--write',action='store_true')
    c=s.add_parser('catalog');c.add_argument('--check',action='store_true')
    r=s.add_parser('redraw');r.add_argument('report');r.add_argument('--output',required=True)
    a=p.parse_args()
    if a.action=='list':print('\n'.join(m['report_id'] for _,m in entries()))
    elif a.action=='verify':print(json.dumps(verify(a.write),ensure_ascii=False,indent=2))
    elif a.action=='catalog':
        verify()
        target=ROOT/'报告目录.md'
        if a.check:assert target.read_text(encoding='utf-8')==catalog_text(),'报告目录已过期'
        else:target.write_text(catalog_text(),encoding='utf-8',newline='\n')
        print('catalog passed')
    else:redraw(a.report,a.output)
