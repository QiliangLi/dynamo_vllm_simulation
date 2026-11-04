"""Archive the 2026-11-02 six-case disk study without publishing raw events."""
import argparse
import csv
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
from zoneinfo import ZoneInfo

REPO=Path(__file__).resolve().parents[2]
read=lambda p:json.loads(p.read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,value):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def table(headers,rows):
    return '|'+ '|'.join(headers)+'|\n|'+'|'.join('---' for _ in headers)+'|\n'+''.join('|'+ '|'.join(map(str,row))+'|\n' for row in rows)

def archive(matrix,reports):
    plan=read(matrix/'plan.json');attempts=read(matrix/'attempts.json');progress=read(matrix/'progress.json')
    assert progress['accepted_formal']==30 and progress['accepted_smokes']==6 and progress['status']=='completed'
    now=datetime.now(ZoneInfo('Asia/Shanghai')).replace(microsecond=0)
    tag=now.strftime('%Y%m%d-%H%M%S')+'_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1'
    study='independent-disk-five-arm-20261102';folders={};manifests={};summaries={}
    schemes=[dict(id=a,label=l,ordering='fcfs' if a=='direct-fcfs' else 'edf',alpha=v,
                  force_admission=False) for a,l,v in (
        ('direct-fcfs','直接准入 FCFS',None),('direct','直接准入 EDF',None),
        ('old-tfs','旧 TFS EDF',None),('tfs-a01','新 TFS EDF α=0.1',.1),('tfs-a1','新 TFS EDF α=1',1.))]
    for case in plan['layouts']:
        src=reports/case;receipt=read(src/'report_data/validation.json')
        assert receipt['status']=='passed' and receipt['visual_reviewed'] and receipt['figure_count']==8
        for rel,expected in receipt['output_files'].items():assert sha(src/rel)==expected['sha256'],rel
        dest=REPO/'report'/case/tag;assert not dest.exists();dest.mkdir(parents=True)
        folders[case]=dest;mapping={};source_files={};figures=[];compaction=None
        for p in sorted(src.rglob('*')):
            if not p.is_file() or '__pycache__' in p.parts:continue
            rel=p.relative_to(src).as_posix();raw=p.read_bytes();source_files[rel]=dict(sha256=sha(p),bytes=len(raw))
            if rel=='report_data/dataset.json':
                from plotting.dataset import validate_dataset
                from plotting.report import metrics,check_reference
                data=json.loads(raw);removed=0
                for c in data['cases']:
                    for interval in c['bandwidth']['intervals']:
                        for key in ('disk_actual_Bps','disk_demand_Bps'):
                            if key in interval:del interval[key];removed+=1
                # The figures use exact aggregate events plus the separately preserved
                # 10ms per-disk integrals. Repeated per-event disk arrays are redundant.
                validate_dataset(data);proof=check_reference(metrics(data),src)
                raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')+b'\n'
                compaction=dict(status='passed',removed_fields=['disk_actual_Bps','disk_demand_Bps'],
                    removed_array_count=removed,source_sha256=source_files[rel]['sha256'],
                    compact_uncompressed_sha256=hashlib.sha256(raw).hexdigest(),
                    preserved='all scalar aggregate events, raw/translated clocks, per-disk 10ms integrals, summaries and request/compute/IO records',
                    original_run_summary_comparison=proof)
            target='report_data/source_validation.json' if rel=='report_data/validation.json' else rel
            if len(raw)>512*1024 and p.suffix in ('.json','.csv'):
                raw=gzip.compress(raw,compresslevel=6,mtime=0);target+='.gz'
            mapping[rel]=target;q=dest/target;q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(raw)
            if p.suffix=='.png':
                width,height=struct.unpack('>II',raw[16:24]);figures.append(dict(path=target,source_sha256=sha(p),width=width,height=height))
        # Small audit receipts are retained; token arrays, raw steps and RPC logs are not.
        own=[r for r in attempts if r['case']==case]
        write(dest/'report_data/execution_attempts.json',own)
        for name in ('manifest.json','experiment.json','profiles.json','frozen.json'):
            shutil.copy2(matrix/case/name,dest/'report_data'/('execution_'+name))
        write(dest/'report_data/matrix_plan.json',plan)
        write(dest/'report_data/dataset_compaction.json',compaction)
        for row in own:
            if row['returncode']!=0 or row['smoke']:continue
            run=matrix/case/f"package/results/{row['run_name']}/run"
            for p in run.glob('*check.json'):
                if p.stat().st_size>512*1024:continue
                q=dest/'report_data/run_checks'/row['arm']/p.name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
        failure=dict(audit='disk-matrix-20261102-r1',phase='synthetic old-tfs smoke',
            error='no observed native load reservation',cause='1-second sampling missed subsecond positive load',
            evidence='native selector raw_prefill_blocks: 21.750',
            correction='positive native raw-prefill log accepted as additional activity evidence; native score comparison, minimum selection and zero final ledger still required',
            strategy_changed=False,polling_changed=False,full_regressions_passed=336,
            formal_runs_before_correction=0,r2_attempt_failures=sum(r['returncode']!=0 for r in attempts))
        write(dest/'report_data/retained_failure_summary.json',failure)
        with (src/'report_data/run_summary.csv').open(encoding='utf-8-sig') as stream:summaries[case]=list(csv.DictReader(stream))
        text=(src/'report.md').read_text(encoding='utf-8')
        text=text.replace('|最大重读持有|','|获准事件峰值重读持有|')
        text=text.replace('这些统计不能单独把TTFT差异归因于RPC。',
            '获准数的口径：网关模式为全部请求生成reservation；Scheduler模式只为重读生成permit，因此两者的获准数不能直接当作同口径重读请求数。表中峰值仅取获准事件的held_after，不包含单独阈值更新引起的持有量变化，也不代表实际在途IO并发。直接准入虽然记录持有量，但不受cap限制。最终cap不等于全程固定cap。\n\n这些统计不能单独把TTFT差异归因于RPC。')
        def link(match):
            value=match.group(1);return ']('+mapping.get(value,value)+')'
        text=re.sub(r'\]\(([^)]+)\)',link,text)
        text=text.replace('python reproduce/generate_report.py --dataset report_data/dataset.json --output ../reproduced-report',
            f'python scripts/manage_reports.py redraw "{case}/{tag}" --output <新的输出目录>')
        text=text.replace('在报告目录执行：','从仓库根目录执行（自动解压标准dataset到临时目录）：')
        prefix=f"\n报告创建时间：{now.isoformat()}，Asia/Shanghai。实际运行起止时间见[全部尝试](report_data/execution_attempts.json)。\n\n本任务新跑五种策略各一次，共1800正式请求和360预热请求。本次归档及离线制图没有再运行性能实验。原始大日志留在本机。公开标准dataset省去冗余的逐事件盘速率数组，保留总速率事件和逐盘10ms积分表；重算校验与原汇总一致，图像字节不变，详见[精简校验](report_data/dataset_compaction.json)。其余较大派生表使用无损gzip压缩。\n\n[Benchmark合同](../../../benchmark/{case}/README.md) · [本次分盘合同](../../../docs/独立多盘五方案矩阵-20261102.md) · [归档清单](manifest.json) · [归档校验](archive_validation.json)\n"
        highest=max(float(r['slo_pct']) for r in summaries[case])
        prefix+=f"\n本组最高SLO满足率为{highest:.2f}%，对应仍有{360-round(highest/100*360)}条请求未达标。相对改善不等于已达到较高的绝对达标率。SLO按每条请求的`deadline=arrival+4×T0`判断；T0使用冻结模型及总参考带宽估计，不按分盘后的实际执行时间放宽。\n"
        lines=text.splitlines(keepends=True);text=lines[0]+prefix+''.join(lines[1:])
        (dest/'report.md').write_text(text,encoding='utf-8')
        (dest/'reproduce/README.md').write_text(f'''# 重绘与重跑

本目录保存已验收的新运行报告。标准dataset、配置、逐请求表、真实batch表、准入表、逐盘带宽表和来源SHA已公开；大规模原始层/RPC事件、依赖和原生扩展不公开。

从仓库根目录执行：

```bash
python scripts/manage_reports.py redraw "{case}/{tag}" --output <新的输出目录>
```

该命令只解压并读取冻结标准dataset，不再运行性能实验。公开dataset省去重复的逐事件盘数组，总速率事件与逐盘10ms积分表完整保留；与源CSV的汇总指标一致，见dataset_compaction.json。随附reproduce目录保留本次固定绘图工具，手动使用时需先解压dataset.json.gz。新图需重新检查，不能沿用原图的视觉验收。

完整性能重跑需准备主README中的冻结运行环境，再使用独立多盘五方案矩阵说明。原benchmark默认仍是共享池，必须显式指定本报告拓扑。图8来自记录提交时刻的冻结存储模型重放，逐次完成和每盘字节积分已核对；仅有公开dataset不能重新审计未发布的原始事件。

source_validation.json验证源生成目录；archive_validation.json验证公开压缩归档。manifest中的simulations_run=0专指归档阶段；source_execution记录本任务实际执行的五次正式运行。
''',encoding='utf-8')
        bm=read(REPO/'benchmark'/case/'manifest.json');selected=[r for r in own if not r['smoke'] and r['returncode']==0]
        manifests[case]=dict(schema_version=1,report_id=case+'/'+tag,study_id=study,benchmark_id=case,
            related_benchmark_ids=list(plan['layouts']),title=text.splitlines()[0].lstrip('# '),
            created_at=now.isoformat(),timezone='Asia/Shanghai',experiment_dates=sorted({r['started_at'][:10] for r in selected}),
            schemes=schemes,compatibility='current_multidisk_five_arm',
            source=dict(report_path=str(src),report_sha256=source_files['report.md']['sha256'],
                        delivery_base='d693b79c737658490f5fc87f349dfa7368ebe27a',
                        working_tree_fingerprint_file='report_data/execution_frozen.json'),
            source_files=source_files,figures=figures,standard_dataset=mapping['report_data/dataset.json'],
            source_visual_reviewed=True,source_figures_unchanged=True,
            changes=['公开归档元信息与相对链接','明确网关/Scheduler许可计数和获准事件峰值的不同口径','标准dataset省去冗余逐事件盘数组，保留逐盘积分表；汇总与原CSV一致','较大派生JSON/CSV用gzip无损压缩','增加小型运行验收凭证及来源'],
            source_dataset_unchanged=False,dataset_compaction='report_data/dataset_compaction.json',
            simulations_run=0,performance_recomputed=False,
            source_execution=dict(new_in_this_task=True,formal_runs=5,formal_requests=1800,warmup_requests=360,
                                  accepted_smokes=1,repeats_per_scheme=1,execution_attempts='report_data/execution_attempts.json'),
            benchmark_input_sha256=bm['original_trace_sha256'],large_raw_events_included=False,files={})
    first=next(iter(folders));overview=folders[first]/'overview.md'
    text='# 独立多盘六负载、五策略总览\n\n'
    text+='本任务完成30次新的正式运行，共10800条正式请求、2160条预热请求。六组冒烟先通过。每种组合只有一次有效运行，没有跨重复置信区间。所有TFS均用EDF；只有直接准入FCFS使用FCFS。\n\n'
    text+='|负载|独立磁盘|SLO最高方案|SLO%|完成最快方案|完成ms|完整报告|\n|---|---|---|---:|---|---:|---|\n'
    detail=[]
    for case,folder in folders.items():
        rows=summaries[case];best=max(rows,key=lambda r:float(r['slo_pct']));fast=min(rows,key=lambda r:float(r['completion_ms']))
        layout=plan['layouts'][case];link='../'+folder.relative_to(REPO/'report').as_posix()+'/report.md'
        # overview is two directories below report; use ../../ to reach the report root.
        link='../../'+folder.relative_to(REPO/'report').as_posix()+'/report.md'
        text+=f"|{case}|{layout['disks']}×{layout['disk_GBps']} GB/s|{best['label']}|{float(best['slo_pct']):.2f}|{fast['label']}|{float(fast['completion_ms']):.1f}|[查看]({link})|\n"
        base=next(r for r in rows if r['mode']=='direct')
        for row in rows:detail.append([case,row['label'],f"{float(row['slo_pct']):.2f}",f"{float(row['slo_pct'])-float(base['slo_pct']):+.2f}",f"{float(row['completion_ms']):.1f}",f"{float(row['mean_ttft_ms']):.1f}",f"{float(row['model_compute_pct']):.2f}"])
    text+='\n## 五方案完整结果\n\n'+table(['负载','方案','SLO%','相对直接EDF pp','完成ms','平均TTFT ms','名义计算%'],detail)
    text+='\n## 如何解释\n\n每份报告含8张图：SLO、在线轻重时间线、名义计算占比、总带宽、总TTFT CDF、分组CDF、到达迟到、逐盘带宽。图8用于看磁盘热点；图4的总带宽不能代替单盘压力。\n\n'
    text+='本轮单次记录中，旧TFS的SLO在两份Mooncake ABC、Mooncake ABCD 90 GB/s及人工ABC中最高；新TFS α=0.1在AgentX和Mooncake ABCD 100 GB/s中最高。AgentX的两者差距只有1条达标请求，不能据此认定稳定优势。六组工况的最佳SLO仍均低于50%。\n\n'
    text+='100 GB/s与90 GB/s分别使用4盘和3盘，也分别运行一次，包含独立的路由和宿主时序；出现完成时间倒挂时，不能仅归因于总带宽。当前矩阵没有同拓扑、多次重复的带宽单变量定标。\n\n'
    text+='本次只把已有HRW多盘存储模型接入冻结实验入口，没有新增逐盘准入或路由。TFS仍按总参考带宽执行全局配额。新旧TFS同时区别准入位置和轻重分类时机；α=1不能作为“等价旧TFS”的依据。\n\n'
    text+='AgentX和人工ABC保留direct_sum；四份Mooncake保留actual_batch_work_v2。不同负载不合并CDF、不汇总总体胜率。AgentX有39条上下文公式外推请求。红色不是纯存储等待，灰色不等于实例没有排队请求，名义计算占比不是硬件利用率。\n\n'
    text+='## 验证与保留的失败\n\n336项完整回归、6组冒烟及30组正式验收通过。r1曾因1秒指标采样遗漏短冒烟的正预留而失败；已保留原因与原生raw_prefill_blocks证据，修正验收后从r2开始全部正式运行。每次有效运行检查输入、有效配置、模型公式、层屏障、真实队列、原生路由、额度归零、RPC排空与进程退出。带宽重建另核对每次读取完成和逐盘字节积分。所有新图逐张检查，公开压缩归档再做SHA、表格、图像和链接校验。\n\n'
    text+='本地保留大型原始事件；GitHub只发布报告、标准派生数据、小型验收凭证和固定重绘工具。每份manifest的source_execution是实际性能运行；simulations_run=0仅指后续归档没有再跑实验。\n'
    overview.write_text(text,encoding='utf-8')
    for case,folder in folders.items():
        m=manifests[case]
        if case==first:m['overview']='overview.md'
        m['files']={p.relative_to(folder).as_posix():dict(sha256=sha(p),bytes=p.stat().st_size) for p in sorted(folder.rglob('*')) if p.is_file()}
        write(folder/'manifest.json',m)
    result=dict(status='archived_pending_repository_verify',created_at=now.isoformat(),study_id=study,
        report_ids=[m['report_id'] for m in manifests.values()],overview=overview.relative_to(REPO).as_posix())
    print(json.dumps(result,ensure_ascii=False,indent=2));return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--matrix',type=Path,required=True);p.add_argument('--reports',type=Path,required=True)
    a=p.parse_args();archive(a.matrix.resolve(),a.reports.resolve())
