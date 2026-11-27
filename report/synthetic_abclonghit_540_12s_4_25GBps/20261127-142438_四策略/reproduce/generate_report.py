#!/usr/bin/env python3
"""从冻结日志或标准数据生成七图、中文报告与校验包；不运行实验。"""
import argparse
import csv
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from redraw_timeline import runtime_command


def delegate(args):
    prefix=['wsl.exe','--distribution',os.environ['REPORT_ORGANIZER_WSL_DISTRO'],'--exec']
    def path(value):
        if value.startswith('/') and not value.startswith('//'):return value
        return subprocess.check_output([*prefix,'wslpath','-a',str(Path(value).resolve())],text=True,encoding='utf-8').strip()
    env=[k+'='+os.environ[k] for k in ('REPORT_ORGANIZER_PYTHON',) if os.environ.get(k)]
    command=[*prefix,'env',*env,'python3',path(str(Path(__file__).resolve()))]
    if args.report:command.append(path(args.report))
    for key in ('manifest','dataset','output','narrative','finalize'):
        if getattr(args,key):command.extend(['--'+key,path(getattr(args,key))])
    for key in ('dpi','bin_ms'):command.extend(['--'+key.replace('_','-'),str(getattr(args,key))])
    if args.visual_reviewed:command.append('--visual-reviewed')
    if args.validate_only:command.append('--validate-only')
    return subprocess.call(command)


def csv_rows(path,rows):
    if not rows:return
    fields=list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',nargs='?',help='现有报告目录、report_data目录或单个run目录')
    parser.add_argument('--manifest',help='跨目录方案清单；与时间线脚本共用格式')
    parser.add_argument('--dataset',help='schema_version=1的标准重绘数据')
    parser.add_argument('--output',help='新输出目录；默认输入旁的generated_report，不覆盖已有文件夹')
    parser.add_argument('--narrative',help='人工分析JSON，可提供0/6/7/8/10章节正文')
    parser.add_argument('--dpi',type=int,default=160)
    parser.add_argument('--bin-ms',type=float,default=10)
    parser.add_argument('--validate-only',action='store_true',help='只校验输入与指标，不出图')
    parser.add_argument('--finalize',help='人工看图后，复核已有产物并更新ZIP')
    parser.add_argument('--visual-reviewed',action='store_true',help='仅配合--finalize，声明已人工检查本次PNG')
    args=parser.parse_args()
    if sum(bool(x) for x in (args.report,args.manifest,args.dataset,args.finalize))!=1:parser.error('请选择报告目录、--manifest、--dataset或--finalize之一。')
    if args.visual_reviewed and not args.finalize:parser.error('--visual-reviewed仅用于人工看图后的--finalize。')
    if not 72<=args.dpi<=600 or not 0<args.bin_ms<=1000:parser.error('dpi需为72～600，桶宽需为0～1000ms。')
    if os.name=='nt' and os.environ.get('REPORT_ORGANIZER_WSL_DISTRO'):return delegate(args)
    python=runtime_command() if not args.validate_only else None
    if python:return subprocess.call([python,str(Path(__file__).resolve()),*sys.argv[1:]],env={**os.environ,'MPLBACKEND':'Agg'})
    from plotting.dataset import native_dataset,validate_dataset
    from plotting.timeline import read_json,digest,require
    from plotting.report import (write_json,metrics,check_reference,render_figures,markdown,
                                 inspect_outputs,package)
    if args.finalize:
        require(args.visual_reviewed,'请先检查本次全部PNG，再传入--visual-reviewed。')
        receipt=inspect_outputs(Path(args.finalize),visual_reviewed=True)
        print(json.dumps(dict(status=receipt['status'],visual_reviewed=True,archive=package(args.finalize)),ensure_ascii=False));return 0
    started=time.perf_counter()
    if args.dataset:
        data=read_json(args.dataset);base=Path(args.dataset).resolve().parent
    else:
        data=native_dataset(args.report,args.manifest)
        base=Path(args.report or args.manifest).resolve()
        if base.is_file():base=base.parent
    data=validate_dataset(data)
    result=metrics(data,args.bin_ms)
    reference=check_reference(result,args.report)
    if args.validate_only:
        print(json.dumps(dict(status='passed',simulations_run=0,cases=len(data['cases']),
            requests=sum(s['requests'] for s in result['summary']),reference=reference,elapsed_s=time.perf_counter()-started),ensure_ascii=False));return 0
    narrative=read_json(args.narrative) if args.narrative else data.get('narrative',{})
    require(set(narrative)<=set(('0','6','7','8','10')) and all(isinstance(v,str) for v in narrative.values()),'人工分析只能提供0/6/7/8/10章节字符串。')
    data['narrative']=narrative
    output=Path(args.output).resolve() if args.output else base/'generated_report'
    require(not output.exists(),'输出目录已经存在。请指定新的--output；脚本保留已有正文和图片。')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.report-',dir=output.parent) as folder:
        stage=Path(folder)/'bundle';stage.mkdir();reports=stage/'report_data';reports.mkdir()
        generated,skipped,colors=render_figures(data,result,stage/'figure',args.dpi)
        write_json(reports/'dataset.json',data)
        write_json(reports/'configuration.json',{c['summary']['case']:dict(config=c.get('config',{}),run_metadata=c.get('run_metadata',{})) for c in data['cases']})
        for name,key in [('run_summary','summary'),('requests','requests'),('device_utilization','workers'),('cdf','cdf'),('cohort_summary','cohorts'),('bandwidth_bins','bins')]:csv_rows(reports/(name+'.csv'),result[key])
        for name,rows in data.get('auxiliary_tables',{}).items():
            require(re.fullmatch(r'[a-z0-9_-]+',name) and name not in ('run_summary','requests','device_utilization','cdf','cohort_summary','bandwidth_bins','layers'), '辅助表名称无效或覆盖主表。')
            csv_rows(reports/(name+'.csv'),rows)
        csv_rows(reports/'layers.csv',[r for c in data['cases'] for r in c.get('layers',[])])
        (stage/'report.md').write_text(markdown(data,result,generated,skipped,narrative),encoding='utf-8')
        reproduce=stage/'reproduce';(reproduce/'plotting').mkdir(parents=True)
        source=Path(__file__).resolve().parent
        for file in ('generate_report.py','redraw_timeline.py'):shutil.copy2(source/file,reproduce/file)
        for file in ('__init__.py','timeline.py','report_style.py','dataset.py','report.py'):shutil.copy2(source/'plotting'/file,reproduce/'plotting'/file)
        # Compare every source hash again, including the interval logs used by the adapter.
        if not args.dataset:
            for case in data['cases']:
                for name,expected in case['sources'].items():require(digest(Path(name))==expected['sha256'],f'源日志在整理期间发生变化：{name}')
        from PIL import Image
        dimensions={}
        for name in generated:
            with Image.open(stage/'figure'/name) as im:dimensions[name]=list(im.size)
        receipt=dict(producer='state-aware-report-v1',schema_version=1,status='data_validated_visual_pending',
            simulations_run=0,visual_reviewed=False,cases=len(data['cases']),requests=sum(s['requests'] for s in result['summary']),
            figure_count=len(generated),image_pixels=dimensions,skipped=skipped,colors=colors,reference=reference,
            elapsed_s=time.perf_counter()-started,bin_ms=args.bin_ms,
            checks=['逐请求SLO与计数','nearest-rank分位数及rank/n经验CDF','实例计算/红色IO/灰色时间账本','可用带宽事件与分桶积分','来源及产物SHA256','Markdown相对链接','ZIP CRC及逐文件内容'],
            source_files={c['summary']['case']:c.get('sources',{}) for c in data['cases']},
            dataset_input=None if not args.dataset else dict(path=str(Path(args.dataset).resolve()),sha256=digest(Path(args.dataset))),
            semantics=dict(cohorts='input cohort; distinct from online heavy/light',compute='nominal model time; not measured hardware utilization',aggregation='none; one independent frozen run per case'),
            output_files={str(p.relative_to(stage)):dict(sha256=digest(p),bytes=p.stat().st_size) for p in sorted(stage.rglob('*')) if p.is_file()})
        write_json(reports/'validation.json',receipt)
        inspect_outputs(stage)
        os.replace(stage,output)
    archive=package(output)
    print(json.dumps(dict(status=receipt['status'],output=str(output),figures=len(generated),reference=reference,
        simulations_run=0,elapsed_s=time.perf_counter()-started,archive=archive),ensure_ascii=False))
    return 0


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    try:sys.exit(main())
    except (ValueError,KeyError,OSError,RuntimeError,ImportError,json.JSONDecodeError) as error:
        print(f'报告生成失败：{error}',file=sys.stderr);sys.exit(2)
