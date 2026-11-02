"""Deterministic report metrics, seven figures, Markdown and verified packaging."""
import csv
import json
import math
import re
import statistics
import textwrap
import unicodedata
import zipfile
from pathlib import Path

from .timeline import csvout, digest, read_json, require, render as timeline_render
from . import report_style as style

FIGURES = ['01_slo_bar','02_full-sort-ms','03_util_box_heatmap','04_bandwidth',
           '05_cdf','06_cdf_by_class','07_arrival_lateness']
PALETTE = ['#888888','#C44E52','#DD8452','#55A868','#4C72B0','#9974A5','#937860','#DA8BC3','#64B5CD']


def wrapped(text, width=16):
    lines=[];current='';used=0
    # Keep FCFS/EDF/alpha values intact and place a suffix on its own line.
    for char in re.findall(r'[A-Za-z0-9α=._/-]+|[\u4e00-\u9fff]+|.',text):
        size=sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in char)
        if char=='＋' and current:
            lines.append(current.strip());current='';used=0
        if current and used+size>width:
            lines.append(current.strip());current='';used=0
        current+=char;used+=size
    if current:lines.append(current.strip())
    return '\n'.join(lines)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def percentile(values, q):
    return sorted(values)[max(0,math.ceil(len(values)*q)-1)]


def bandwidth_bins(band, horizon, width):
    """Exact overlap integration, not arithmetic mean of unequal event intervals."""
    n = math.ceil(horizon/width); actual = [0.]*n; demand = [0.]*n
    for row in band['intervals']:
        a,b = row['start_s'],row['end_s']
        for i in range(max(0,int(a/width)),min(n,math.ceil(b/width))):
            dt = max(0.,min(b,(i+1)*width)-max(a,i*width))
            actual[i] += dt*row['actual_Bps']; demand[i] += dt*row['demand_Bps']
    require(abs(math.fsum(actual)-band['read_bytes']) <= band['integral_tolerance_bytes'], '分桶后带宽积分不守恒。')
    return [dict(bin_mid_ms=(i+.5)*width*1000,bin_width_ms=width*1000,
                 demand_GBps=demand[i]/width/1e9,actual_GBps=actual[i]/width/1e9) for i in range(n)]


def metrics(data, bin_ms=10):
    summaries=[]; requests=[]; cdf=[]; workers=[]; cohorts=[]; bins=[]
    horizon=math.ceil(max(c['summary']['span_s'] for c in data['cases'])*1.03*10)/10
    for case in data['cases']:
        s=case['summary']; key=s['case']; rr=case['requests']; span=s['span_s']
        values=[(r['completion_s']-r['arrival_s'])*1000 for r in rr]
        row=dict(mode=key,label=s['label'],requests=len(rr),workers=s['workers'],completion_ms=span*1000,
                 mean_ttft_ms=statistics.mean(values),p50_ms=percentile(values,.5),p95_ms=percentile(values,.95),
                 slo_pct=100*sum(r['completion_s']<=r['deadline_s'] for r in rr)/len(rr),
                 model_compute_s=s['model_s'],model_compute_pct=None if s['model_s'] is None else s['model_s']/s['workers']/span*100)
        for name in ('gateway_wait_ms','arrival_lateness_ms'):
            row['mean_'+name]=statistics.mean(r[name] for r in rr) if all(name in r for r in rr) else None
        for r,v in zip(rr,values):
            requests.append(dict(mode=key,**r,ttft_ms=v,slo_limit_ms=(r['deadline_s']-r['arrival_s'])*1000,
                                 slo_ok=int(r['completion_s']<=r['deadline_s'])))
        groups=['all']+sorted({r['cohort'] for r in rr})
        require('all' not in {r['cohort'] for r in rr}, 'cohort名称all为总体保留。')
        for group in groups:
            subset=rr if group=='all' else [r for r in rr if r['cohort']==group]
            vv=sorted((r['completion_s']-r['arrival_s'])*1000 for r in subset)
            for rank,v in enumerate(vv,1):cdf.append(dict(mode=key,group=group,rank=rank,n=len(vv),ttft_ms=v,cdf=rank/len(vv)))
            cohorts.append(dict(mode=key,group=group,n=len(vv),mean_ttft_ms=statistics.mean(vv),p50_ms=percentile(vv,.5),
                                p95_ms=percentile(vv,.95),slo_pct=sum(r['completion_s']<=r['deadline_s'] for r in subset)/len(vv)*100))
        for w in case['accounts']:
            workers.append(dict(**w,completion_s=span,model_compute_pct=w['model_s']/span*100))
        band=case.get('bandwidth')
        if band is not None:
            cap=band['capacity_Bps']; intervals=band['intervals']
            row.update(mean_bandwidth_GBps=band['read_bytes']/span/1e9,
                demand_oversubscribed_pct=sum((e['end_s']-e['start_s']) for e in intervals if e['demand_Bps']>cap)/span*100,
                saturated_pct=sum((e['end_s']-e['start_s']) for e in intervals if e['actual_Bps']>=.9*cap)/span*100)
            bins.extend(dict(mode=key,**b) for b in bandwidth_bins(band,horizon,bin_ms/1000))
        else: row.update(mean_bandwidth_GBps=None,demand_oversubscribed_pct=None,saturated_pct=None)
        summaries.append(row)
    return dict(summary=summaries,requests=requests,cdf=cdf,workers=workers,cohorts=cohorts,bins=bins,horizon=horizon)


def check_reference(result, report=None):
    """Compare derived values to published statistics, when those fields exist."""
    if report is None:return dict(status='not_available',reason='标准数据输入未指定原报告；仅执行内部一致性校验。')
    root=Path(report); path=root/'report_data/run_summary.csv'
    if not path.is_file():return dict(status='not_available',reason='输入目录没有已发布run_summary.csv。')
    with path.open(encoding='utf-8-sig',newline='') as stream: old={r['mode']:r for r in csv.DictReader(stream)}
    checks=0
    for row in result['summary']:
        require(row['mode'] in old, '原汇总表缺少方案。')
        for key in ('completion_ms','mean_ttft_ms','p50_ms','p95_ms','slo_pct','model_compute_s','model_compute_pct','mean_bandwidth_GBps','saturated_pct'):
            value=old[row['mode']].get(key)
            if value not in (None,'') and row[key] is not None:
                require(math.isclose(float(value),row[key],rel_tol=1e-9,abs_tol=1e-6),f"{row['mode']} 原报告指标不一致：{key}")
                checks+=1
    device_path=root/'report_data/device_utilization.csv'
    if device_path.is_file():
        with device_path.open(encoding='utf-8-sig',newline='') as stream:
            prior={(r['mode'],int(r['worker'])):r for r in csv.DictReader(stream)}
        for row in result['workers']:
            key=(row['case'],row['worker']);require(key in prior,'原实例账本缺少实例。')
            for current,previous in [('model_s','model_compute_s'),('red_s','io_without_model_compute_s'),('gray_s','other_s')]:
                if prior[key].get(previous) not in (None,''):
                    require(abs(row[current]-float(prior[key][previous]))<1e-7,f'{key} 实例时间账本与原报告不一致：{current}')
                    checks+=1
    return dict(status='passed',numeric_checks=checks,source=str(path),sha256=digest(path),
                note='只比较列出的同口径数值，不代表模型公式或运行实现已重新验收。')


def render_figures(data, result, output, dpi=160):
    style.configure()
    import matplotlib.pyplot as plt
    import numpy as np
    cases=data['cases']; out=Path(output); out.mkdir(parents=True,exist_ok=True)
    labels=[c['summary']['label'] for c in cases]; colors=[c.get('color') or PALETTE[i%len(PALETTE)] for i,c in enumerate(cases)]
    summaries=result['summary']; count=len(cases); generated=[]; skipped={}
    def save(fig,index):
        name=FIGURES[index]+'.png'; fig.savefig(out/name,dpi=dpi,facecolor='white');plt.close(fig);generated.append(name)
    def ecdf(ax,values,color,label,ls='-'):
        values=sorted(values)
        if values:ax.step([min(0,values[0]),*values],[0,*[(i+1)/len(values) for i in range(len(values))]],where='post',color=color,label=label,ls=ls,lw=1.4)
    fig,ax=plt.subplots(figsize=(max(11.8,count*2.2),6.2))
    for i,(s,color) in enumerate(zip(summaries,colors)):
        ax.bar(i,s['slo_pct'],color=color,width=.6);ax.text(i,s['slo_pct']+1.5,f"{s['slo_pct']:.1f}%",ha='center')
    ax.set(xticks=range(count),xticklabels=[wrapped(l,20) for l in labels],ylim=(0,105),ylabel='SLO 满足率（%）',title='图1｜逐请求匹配期限；各方案独立统计')
    ax.grid(axis='y',alpha=.16);ax.set_axisbelow(True);fig.tight_layout();save(fig,0)
    available=[c for c in cases if c.get('comp') is not None]
    if available:
        timeline_render(available,out/(FIGURES[1]+'.png'),'图2｜'+data['title']+'｜在线轻重时间线',dpi);generated.append(FIGURES[1]+'.png')
        wide=max(14,max(c['summary']['workers'] for c in available)*.25+7)
        if len(available)>8:
            # 多方案时左右并排会把箱线图压得过窄：改为上下堆叠，箱线图占全宽。
            fig,(box,heat)=plt.subplots(2,1,figsize=(wide,max(10.6,len(available)*.42+8)),gridspec_kw={'height_ratios':[1,1.2]})
        else:
            fig,(box,heat)=plt.subplots(1,2,figsize=(wide,max(5.3,len(available)*.52+2)),gridspec_kw={'width_ratios':[1,1.6]})
        values=[[a['model_s']/c['summary']['span_s']*100 for a in c['accounts']] for c in available]
        bp=box.boxplot(values,patch_artist=True,showmeans=True,meanprops=dict(marker='D',markerfacecolor=style.UTIL_MEAN_COLOR,markeredgecolor=style.UTIL_MEAN_COLOR),medianprops=dict(color='black'))
        for patch in bp['boxes']:patch.set(facecolor=style.UTIL_BOX_COLOR,alpha=.8)
        box_labels=[c['summary']['label'] for c in available] if len(available)>8 else [wrapped(c['summary']['label'],12) for c in available]
        box.set(xticks=range(1,len(available)+1),xticklabels=box_labels,ylim=(0,100),ylabel='名义计算时间 / 本方案完成窗口（%）',title='各实例计算占比分布');box.tick_params(axis='x',labelsize=8)
        if len(available)>8: box.tick_params(axis='x',rotation=90)
        box.grid(axis='y',alpha=.16)
        matrix=np.full((len(available),max(len(v) for v in values)),np.nan)
        for i,v in enumerate(values):matrix[i,:len(v)]=v
        cmap=plt.get_cmap(style.HEATMAP['cmap']).copy();cmap.set_bad('#eeeeee')
        im=heat.imshow(matrix,aspect='auto',cmap=cmap,vmin=0,vmax=100)
        heat.set(xticks=range(0,matrix.shape[1],max(1,math.ceil(matrix.shape[1]/24))),yticks=range(len(available)),yticklabels=[c['summary']['label'] for c in available],xlabel='原实例编号；灰格表示该方案无此实例',title='计算占比热力图')
        fig.colorbar(im,ax=heat,label='%');fig.suptitle('图3｜名义模型计算占比；不是硬件实测利用率');fig.tight_layout(rect=(0,0,1,.91));save(fig,2)
    for index in (1,2):
        missing=[c['summary']['label'] for c in cases if c.get('comp') is None]
        if missing:skipped[FIGURES[index]]='缺少计算或IO区间：'+','.join(missing)
    available=[c for c in cases if c.get('bandwidth') is not None]
    if available:
        fig,axes=plt.subplots(len(available),1,figsize=(14.6,max(4,2.5*len(available)+1)),sharex=True,sharey=True,squeeze=False)
        maxrate=max([.5]+[max(r['actual_GBps'],r['demand_GBps']) for r in result['bins']]+[c['bandwidth']['capacity_Bps']/1e9 for c in available])
        for ax,c in zip(axes[:,0],available):
            rows=[r for r in result['bins'] if r['mode']==c['summary']['case']]
            for field,color,label in [('demand_GBps',style.BANDWIDTH_COLOR['demand'],'需求 Σ申请率'),('actual_GBps',style.BANDWIDTH_COLOR['actual'],'实际 Σ分配率')]:
                ax.plot([r['bin_mid_ms'] for r in rows],[max(.5,r[field]) for r in rows],color=color,lw=.9,label=label)
            cap=c['bandwidth']['capacity_Bps']/1e9;ax.axhline(cap,color='black',ls='--',lw=1,label=f'容量 {cap:g} GB/s')
            ax.set(yscale='log',ylim=(.5,maxrate*1.4),xlim=(0,result['horizon']*1000),ylabel='GB/s',title=c['summary']['label']);ax.grid(which='both',alpha=.16);ax.legend(loc='upper right',fontsize=8)
        axes[-1,0].set_xlabel('从首条正式请求到达起的模型时间（ms）；对数底线0.5仅用于显示零值')
        fig.suptitle('图4｜区间重叠积分；各方案容量取自本次数据');fig.tight_layout(rect=(0,0,1,.97));save(fig,3)
    missing=[c['summary']['label']+'：'+c.get('bandwidth_unavailable','未提供速率区间') for c in cases if c.get('bandwidth') is None]
    if missing:skipped[FIGURES[3]]='；'.join(missing)
    fig,ax=plt.subplots(figsize=(10.2,max(6,count*.35+3.5)))
    for c,s,color in zip(cases,summaries,colors):
        ecdf(ax,[(r['completion_s']-r['arrival_s'])*1000 for r in c['requests']],color,f"{s['label']}｜p50 {s['p50_ms']:.0f} / p95 {s['p95_ms']:.0f} ms")
    # A single reference deadline CDF is valid only if the per-ID widths match.
    widths=[{r['id']:(r['deadline_s']-r['arrival_s'])*1000 for r in c['requests']} for c in cases]
    same=all(w.keys()==widths[0].keys() and all(abs(w[k]-widths[0][k])<1e-6 for k in w) for w in widths)
    if same:ecdf(ax,list(widths[0].values()),'#444444','共同逐请求期限宽度分布',':')
    ax.set(xlim=(0,None),ylim=(0,1.02),xlabel='网关 TTFT（ms，包含准入等待）',ylabel='累计分布 CDF',title='图5｜总体 TTFT；分位数使用 nearest-rank')
    ax.grid(alpha=.18);ax.legend(loc='lower right',fontsize=8);fig.tight_layout();save(fig,4)
    groups=sorted({r['cohort'] for c in cases for r in c['requests']})
    fig,axes=plt.subplots(1,len(groups),figsize=(max(6,6*len(groups)),max(4.7,count*.3+3)),sharey=True,squeeze=False)
    for ax,group in zip(axes[0],groups):
        for c,color in zip(cases,colors):
            subset=[r for r in c['requests'] if r['cohort']==group]
            ecdf(ax,[(r['completion_s']-r['arrival_s'])*1000 for r in subset],color,c['summary']['label']+f'（n={len(subset)}）')
        ax.set(xlim=(0,None),ylim=(0,1.02),xlabel='网关 TTFT（ms）',title=group);ax.grid(alpha=.18);ax.legend(loc='lower right',fontsize=8)
    axes[0,0].set_ylabel('累计分布 CDF');fig.suptitle('图6｜输入cohort分组；不等同于时间线的在线轻重分类');fig.tight_layout(rect=(0,0,1,.91));save(fig,5)
    available=[(c,color) for c,color in zip(cases,colors) if all('arrival_lateness_ms' in r for r in c['requests'])]
    if available:
        fig,ax=plt.subplots(figsize=(10.2,max(5,count*.3+3)))
        for c,color in available:ecdf(ax,[r['arrival_lateness_ms'] for r in c['requests']],color,c['summary']['label'])
        # Negative lateness is retained if present; TTFT remains nonnegative.
        ax.set(ylim=(0,1.02),xlabel='实际客户端发送−计划发送（ms）',ylabel='累计分布 CDF',title='图7｜客户端投递迟到；不替换网关TTFT计时边界');ax.grid(alpha=.18);ax.legend(loc='lower right',fontsize=8);fig.tight_layout();save(fig,6)
    missing=[c['summary']['label'] for c in cases if not all('arrival_lateness_ms' in r for r in c['requests'])]
    if missing:skipped[FIGURES[6]]='缺少计划与实际发送记录：'+','.join(missing)
    return generated,skipped,dict(zip(labels,colors))


def table(headers, rows):
    def cell(value):return str(value).replace('|','\\|').replace('\n','<br>')
    return '|'+ '|'.join(headers)+'|\n|'+'|'.join(['---']+['---:']*(len(headers)-1))+'|\n'+''.join('|'+ '|'.join(cell(v) for v in row)+'|\n' for row in rows)


def markdown(data, result, figures, skipped, narrative=None):
    narrative=narrative or {}; summaries=result['summary']; best_slo=max(summaries,key=lambda r:r['slo_pct']); fastest=min(summaries,key=lambda r:r['completion_ms'])
    fmt=lambda v:'缺少数据' if v is None else f'{v:.1f}'
    text='# 实验结果：'+data['title']+'\n\n'
    text+=table(['项目','约定'],[
        ['执行边界',data.get('execution_boundary','从冻结日志离线整理；本次没有重跑实验')],
        ['方案顺序',' → '.join(s['label'] for s in summaries)],
        ['输入种子 / 重复','每个case独立展示一个冻结运行；种子与重复见附表，未提供时不推断'],
        ['汇总 / 轨迹','不跨case合并样本、不生成置信区间；轨迹保留全部正式请求'],
        ['SLO / TTFT','逐请求匹配deadline；TTFT=first_token−gateway arrival，包含准入等待'],
        ['分组','分类表和CDF使用输入cohort；时间线使用在线轻重判定'],
        ['单位','时间秒/毫秒；容量字节；带宽GB/s=10^9 B/s'],
        *[[str(k),str(v)] for k,v in data.get('metadata',{}).items()]])
    text+='\n## 0. 总判决\n\n'
    text+=narrative.get('0',f"本组记录中，{best_slo['label']}的SLO满足率最高，为 **{best_slo['slo_pct']:.1f}%**；{fastest['label']}完成最快，为 **{fastest['completion_ms']:.1f} ms**。这只描述给定运行，不能单独证明稳定收益。")+'\n\n'
    text+='## 1. 汇总表与 SLO 柱状图\n\n'
    text+=table(['方案','请求数','完成ms','平均TTFT ms','p95 ms','SLO%','网关等待ms'],[[s['label'],s['requests'],fmt(s['completion_ms']),fmt(s['mean_ttft_ms']),fmt(s['p95_ms']),fmt(s['slo_pct']),fmt(s['mean_gateway_wait_ms'])] for s in summaries])
    labels={s['mode']:s['label'] for s in summaries}
    text+='\n分类结果使用输入cohort：\n\n'+table(['方案','分组','请求数','平均TTFT ms','SLO%'],[[labels[c['mode']],c['group'],c['n'],fmt(c['mean_ttft_ms']),fmt(c['slo_pct'])] for c in result['cohorts'] if c['group']!='all'])
    captions={0:'柱高与上表同口径。每条请求使用自身期限判断是否达标。',
        1:'蓝重读、橙轻读、紫轻重混批。红色为未被名义计算覆盖的在途IO，灰色含空闲、排队和控制。动态与冻结分类分别标注；直接放行分类仅作参考。虚线右侧仅为统一横轴补齐。',
        2:'箱线和热力图来自同一组逐实例数据。分母是各方案从首次正式到达到末条首token的窗口。该值不是硬件实测利用率。',
        3:'橙线为需求，蓝线为实际分配，虚线为本次容量。按时间重叠积分，不对不等长事件直接取平均。零值在CSV保留0。',
        4:'经验CDF按rank/n绘制，终点为1。期限不同时，SLO必须逐请求匹配，不能由两条CDF交点推算。',
        5:'此处分组来自cohort，可能是历史A/B或业务分类，不用该标签推断在线轻重。',
        6:'此图描述客户端实际发送相对计划的迟到，不是单次准入RPC耗时。'}
    def image(index):
        name=FIGURES[index]+'.png'
        if name not in figures:return '\n缺少数据：'+skipped.get(FIGURES[index],'未生成')+'\n\n'
        result='\n!['+FIGURES[index]+'](figure/'+name+')\n\n**读法**：'+captions[index]+'\n\n'
        if FIGURES[index] in skipped:result+='部分方案缺少数据：'+skipped[FIGURES[index]]+'\n\n'
        return result
    text+=image(0)
    for section,title,index in [(2,'各方案 full-sort-ms 执行时间线',1),(3,'模型计算占比分布与热力图',2),(4,'存储带宽时序',3),(5,'TTFT 累积分布 CDF',4)]:
        text+=f'## {section}. {title}\n\n'+image(index)
        if section==4:
            text+=table(['方案','平均GB/s','需求超订%','实际饱和%'],[[s['label'],fmt(s['mean_bandwidth_GBps']),fmt(s['demand_oversubscribed_pct']),fmt(s['saturated_pct'])] for s in summaries])+'\n'
        if section==5:text+=image(5)
    text+='## 6. 机制解释与前序对照\n\n'+narrative.get('6','脚本只生成统计证据。当前未提供人工机制分析或前序受控对照；不从颜色、排名或单次轨迹推断原因。')+'\n\n'
    text+='## 7. 计算模型与结论限制\n\n'+narrative.get('7','保留输入运行的配置与模型假设，见配置附表。名义模型计算时长与宿主实际耗时分开。未聚合多次运行，也未重新验证底层模型公式。标准数据包足以重绘图表，不等同于完整原始运行归档。')+'\n\n'
    text+='## 8. 附表\n\n'+image(6)
    text+=table(['方案','实例数','模型计算占比%','运行标识'],[[s['label'],s['workers'],fmt(s['model_compute_pct']),json.dumps(c['run_metadata'],ensure_ascii=False) if c.get('run_metadata') else '未提供'] for s,c in zip(summaries,data['cases'])])+'\n'
    text+=table(['方案','序列上限','token预算','存储容量GB/s','准入位置 / 规则','计算模型'],[
        [s['label'],c.get('config',{}).get('max_num_seqs','未提供'),c.get('config',{}).get('max_num_batched_tokens','未提供'),
         fmt(c['bandwidth']['capacity_Bps']/1e9) if c.get('bandwidth') else '未提供',
         str(c.get('config',{}).get('admission',{}).get('placement','未提供'))+' / '+str(c.get('config',{}).get('admission',{}).get('classification','未提供')),
         c.get('config',{}).get('compute',{}).get('batch_time_model','未提供')]
        for s,c in zip(summaries,data['cases'])])+'\n'
    text+=narrative.get('8','完整配置和模型参数见[配置附表](report_data/configuration.json)。')+'\n\n'
    text+='## 9. 验证、数据与复现\n\n'
    text+='已校验请求计数、逐请求SLO、nearest-rank分位数、CDF概率质量、实例时间账本及可用带宽积分。图片人工检查状态见校验文件。\n\n'
    text+='- [校验记录](report_data/validation.json)\n- [方案汇总](report_data/run_summary.csv)\n- [请求明细](report_data/requests.csv)\n- [标准重绘数据](report_data/dataset.json)\n- [复现入口](reproduce/generate_report.py)\n\n'
    text+='在报告目录执行：\n\n```bash\npython reproduce/generate_report.py --dataset report_data/dataset.json --output ../reproduced-report\n```\n\n'
    text+='## 10. 简要解读\n\n'+narrative.get('10','先看SLO与完成时间，再结合时间线、计算占比和带宽曲线定位差异。原因判断仍需对应调度、路由和准入日志。')+'\n'
    return text


def inspect_outputs(root, visual_reviewed=False):
    from PIL import Image
    root=Path(root); receipt=read_json(root/'report_data/validation.json')
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    require(actual==set(receipt['output_files'])|{'report_data/validation.json'},'生成目录包含未登记文件或缺少产物。')
    for name,expected in receipt['output_files'].items():
        path=root/name;require(path.is_file() and digest(path)==expected['sha256'],f'生成后文件已变更：{name}；请重新生成或重新校验。')
    for p in (root/'figure').glob('*.png'):
        with Image.open(p) as im:im.verify()
    text=(root/'report.md').read_text(encoding='utf-8')
    require([int(x) for x in re.findall(r'^## (\d+)\.',text,re.M)]==list(range(11)), '报告章节缺失。')
    for link in re.findall(r'\]\(([^)]+)\)',text):
        if '://' not in link and not link.startswith('#'):require((root/link).is_file(),f'报告链接无效：{link}')
    if visual_reviewed:
        receipt['visual_reviewed']=True;receipt['status']='passed'
        write_json(root/'report_data/validation.json',receipt)
    return receipt


def package(root):
    root=Path(root); archive=root.parent/(root.name+'.zip')
    files=[p for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts]
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,str(p.relative_to(root.parent)))
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None,'ZIP CRC失败。')
        require(len(z.infolist())==len(files),'ZIP文件数量不符。')
        for p in files:require(z.read(str(p.relative_to(root.parent)))==p.read_bytes(),'ZIP与本地文件不一致。')
    return dict(path=str(archive),bytes=archive.stat().st_size,sha256=digest(archive),status='passed')
