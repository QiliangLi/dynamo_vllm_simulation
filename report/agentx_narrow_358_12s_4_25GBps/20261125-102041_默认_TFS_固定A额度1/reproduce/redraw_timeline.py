#!/usr/bin/env python3
"""从进程栈日志重绘在线轻重时间线；不启动实验，不修改已有报告。"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]


def runtime_command():
    if all(importlib.util.find_spec(name) is not None for name in ('matplotlib','numpy','PIL')):
        return None
    state=Path(os.environ.get('STATE_AWARE_ENV_HOME',str(Path.home()/'.local/share/state-aware-network')))
    explicit=os.environ.get('STATE_AWARE_PLOT_PYTHON')
    candidates=[Path(explicit)] if explicit else [state/'venvs/dv/bin/python',state/'venvs/sa/bin/python',*sorted((state/'audits').glob('*/venv/bin/python'))]
    for candidate in candidates:
        if not candidate.is_file():continue
        checked=subprocess.run([str(candidate),'-c','import matplotlib,numpy,PIL'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if checked.returncode==0:return str(candidate)
    raise RuntimeError('未找到绘图环境。请安装 matplotlib、numpy、pillow，或用 STATE_AWARE_PLOT_PYTHON 指定已有 Python。')


def windows_delegate(args):
    prefix=['wsl.exe','--distribution',os.environ.get('STATE_AWARE_WSL_DISTRO','Ubuntu-24.04'),'--exec']
    def convert(value):
        if value.startswith('/') and not value.startswith('//'):return value
        return subprocess.check_output([*prefix,'wslpath','-a',str(Path(value).resolve())],text=True,encoding='utf-8').strip()
    script=convert(str(Path(__file__).resolve()))
    forwarded=[name+'='+os.environ[name] for name in ('STATE_AWARE_PLOT_PYTHON','STATE_AWARE_ENV_HOME') if os.environ.get(name)]
    runtime_env=['env',*forwarded] if forwarded else []
    command=[*prefix,*runtime_env,'python3',script]
    if args.report:command.append(convert(args.report))
    if args.manifest:command.extend(['--manifest',convert(args.manifest)])
    if args.output:command.extend(['--output',convert(args.output)])
    if args.title:command.extend(['--title',args.title])
    command.extend(['--dpi',str(args.dpi)])
    if args.validate_only:command.append('--validate-only')
    return subprocess.call(command)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',nargs='?',help='报告目录、report_data目录或单个run目录')
    parser.add_argument('--manifest',help='自定义方案列表的JSON配置文件，支持跨目录组合')
    parser.add_argument('--output',help='输出目录；默认在输入目录下的online_timeline')
    parser.add_argument('--title',help='图标题；不影响数据')
    parser.add_argument('--dpi',type=int,default=160,help='PNG分辨率，默认160')
    parser.add_argument('--validate-only',action='store_true',help='只读取并校验日志，不生成或覆盖图片')
    args=parser.parse_args()
    if bool(args.report)==bool(args.manifest):parser.error('请传入一个报告目录，或使用 --manifest，二选一。')
    if not 72<=args.dpi<=600:parser.error('--dpi 必须在72～600之间。')
    if os.name=='nt':return windows_delegate(args)
    if not args.validate_only:
        python=runtime_command()
        if python:
            return subprocess.call([python,str(Path(__file__).resolve()),*sys.argv[1:]],env={**os.environ,'MPLBACKEND':'Agg'})
    from plotting.timeline import DataError,discover,load_case,render,csvout,digest,require
    begin=time.perf_counter()
    spec=discover(args.report,args.manifest)
    cases=[load_case(case) for case in spec['cases']]
    require(len({case['summary']['case'] for case in cases})==len(cases),'方案ID重复。')
    summaries=[case['summary'] for case in cases]
    if args.validate_only:
        print(json.dumps(dict(status='passed',simulations_run=0,cases=summaries,elapsed_s=time.perf_counter()-begin),ensure_ascii=False))
        return 0
    output=Path(args.output).resolve() if args.output else spec['output']
    output.parent.mkdir(parents=True,exist_ok=True)
    # Render into a sibling temporary directory; failed validation leaves old outputs intact.
    with tempfile.TemporaryDirectory(prefix='.timeline-',dir=output.parent) as temporary:
        staging=Path(temporary)
        render(cases,staging/'timeline.png',args.title or spec['title'],args.dpi)
        csvout(staging/'summary.csv',summaries)
        csvout(staging/'layers.csv',[row for case in cases for row in case['layers']])
        csvout(staging/'requests.csv',[row for case in cases for row in case['requests']])
        csvout(staging/'workers.csv',[row for case in cases for row in case['accounts']])
        from PIL import Image
        with Image.open(staging/'timeline.png') as im:
            require(im.width>0 and im.height>0,'PNG为空。')
            dimensions=list(im.size)
        explanation='''# 在线轻重时间线\n\n蓝色为重读，橙色为轻读，紫色为轻重混批。新TFS使用到达时冻结标记；动态模式使用每层计算开始时的阈值。直接放行的动态分类只作参考，不代表存在额度限制。\n\n红色是未被名义模型计算覆盖的在途IO，不等于纯读取依赖阻塞。灰色包含空闲、排队和控制开销。虚线右侧只为统一横轴补齐。每个方案独立按首次计算排序并保留原实例编号。\n\n此目录只包含离线重绘结果，不重跑实验，不修改源报告或性能数据。summary.csv记录运行窗口和时间账本；layers.csv保存逐层分类；requests.csv保存逐请求分类；workers.csv保存实例时间账本；validation.json保存来源和文件SHA256。\n'''
        (staging/'README.md').write_text(explanation,encoding='utf-8')
        receipt=dict(schema_version=1,status='passed',simulations_run=0,
            cases=summaries,image_pixels=dimensions,elapsed_s=time.perf_counter()-begin,
            source_files={case['summary']['case']:case['sources'] for case in cases},
            output_files={p.name:dict(sha256=digest(p),bytes=p.stat().st_size) for p in staging.iterdir()},
            semantics=dict(compute='nominal model intervals',red='per-worker union of inflight reads minus nominal compute',
                classification='arrival-frozen hints or dynamic controller threshold at each layer start',
                historical_AB_used_for_classification=False))
        (staging/'validation.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        output.mkdir(exist_ok=True)
        for path in staging.iterdir():os.replace(path,output/path.name)
    print(json.dumps(dict(status='passed',output=str(output),image=str(output/'timeline.png'),
        simulations_run=0,cases=len(cases),elapsed_s=time.perf_counter()-begin),ensure_ascii=False))
    return 0


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    try:sys.exit(main())
    except (ValueError,KeyError,OSError,RuntimeError,ImportError,json.JSONDecodeError) as error:
        print(f'重绘失败：{error}',file=sys.stderr)
        sys.exit(2)
