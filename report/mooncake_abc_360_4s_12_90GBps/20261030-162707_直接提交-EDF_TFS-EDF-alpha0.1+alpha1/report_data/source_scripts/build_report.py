"""从紧凑审计数据重画全局4%六组结果；不重新运行仿真。"""
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import zipfile

HERE=Path(__file__).resolve().parent
if (HERE/'report_data').is_dir():
    OUT=HERE
else:
    W=next(p for p in HERE.parents if (p/'workspace.json').is_file())
    OUT=W/'note/tfs_dv/Mooncake全局4%-90GB三方案-20261029'
    OUT.mkdir(parents=True,exist_ok=True)
    shutil.copy2(W/'tooling/skills/report-organizer/scripts/report_style.py',OUT/'report_style.py')
sys.path.insert(0,str(OUT))
import report_style as style
style.configure()
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np

D=OUT/'report_data';FIG=OUT/'figure';FIG.mkdir(exist_ok=True)
TITLE='实验结果-全局4%计算额度-90GB三方案-20261029.md'
ARMS=['direct','tfs-a01','tfs-a1']
LABEL={'direct':'直接准入 EDF','tfs-a01':'新 TFS α=0.1','tfs-a1':'新 TFS α=1'}
COL={'direct':'#888888','tfs-a01':'#4C72B0','tfs-a1':'#55A868'}
STATES=dict(style.STATE_COLOR,D='#8C564B')
read=lambda p:json.loads(p.read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def csvout(name,rows):
    with (D/name).open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def table(headers,rows):
    return '|'+ '|'.join(headers)+'|\n|'+'|'.join(['---']+['---:']*(len(headers)-1))+'|\n'+''.join('|'+ '|'.join(map(str,row))+'|\n' for row in rows)


def img(name,caption):
    return f'\n![{caption}](figure/{name}.png)\n\n**读法**：{caption}\n\n'


def save(fig,name):
    style.save(fig,FIG/(name+'.png'))


def main():
    summary=read(D/'summary.json');checks=read(D/'extraction_checks.json')
    with (D/'requests.csv').open() as f:requests=list(csv.DictReader(f))
    for r in requests:
        for key in ['ttft_ms','client_ttft_ms','deadline_ms','lateness_ms']:
            r[key]=float(r[key])
        r['slo']=int(r['slo'])
    with (D/'device_utilization.csv').open() as f:devices=list(csv.DictReader(f))
    with gzip.open(D/'trajectories.json.gz','rt') as f:trajectories=json.load(f)
    S={(r['dataset'],r['arm']):r for r in summary}
    T={(r['dataset'],r['arm']):r for r in trajectories}
    assert len(summary)==6 and len(requests)==2160 and len(devices)==72
    classes=defaultdict(list);cdf=[];bands=[]
    for dataset in ['ABC','ABCD']:
        for arm in ARMS:
            rr=[r for r in requests if r['dataset']==dataset and r['arm']==arm]
            assert len(rr)==360
            s=S[dataset,arm]
            assert math.isclose(sum(r['ttft_ms'] for r in rr)/360,s['mean_ttft_ms'],rel_tol=1e-12)
            for label in ['all',*list(dataset)]:
                group=rr if label=='all' else [r for r in rr if r['class_id']==label]
                ts=sorted(r['ttft_ms'] for r in group)
                classes[dataset].append(dict(arm=arm,class_id=label,n=len(group),
                    mean_ttft_ms=sum(ts)/len(ts),p95_ms=ts[math.ceil(.95*len(ts))-1],
                    slo_pct=sum(r['slo'] for r in group)/len(group)*100))
                cdf.extend(dict(dataset=dataset,arm=arm,class_id=label,rank=i,n=len(ts),ttft_ms=t,cdf=i/len(ts))
                           for i,t in enumerate(ts,1))
    csvout('cdf.csv',cdf);csvout('class_summary.csv',[dict(dataset=d,**r) for d,rs in classes.items() for r in rs])
    for dataset in ['ABC','ABCD']:
        # 1. SLO柱状图，分类与总体同图。
        groups=['all',*list(dataset)];fig,ax=plt.subplots(figsize=(11.8,5.2));x=np.arange(len(groups));width=.24
        for j,arm in enumerate(ARMS):
            vals=[next(r['slo_pct'] for r in classes[dataset] if r['arm']==arm and r['class_id']==g) for g in groups]
            bars=ax.bar(x+(j-1)*width,vals,width,color=COL[arm],label=LABEL[arm])
            ax.bar_label(bars,labels=[f'{v:.1f}' for v in vals],padding=3,fontsize=8)
        ax.set_xticks(x,['总体' if g=='all' else g for g in groups]);ax.set_ylim(0,110)
        ax.set_ylabel('SLO 达标率（%）');ax.set_title(dataset+'｜逐请求 TTFT ≤ 4×T0')
        ax.legend(loc='upper right');ax.grid(axis='y',alpha=.15);fig.tight_layout();save(fig,f'{dataset}_01_slo')
        # 2. 三方案原生batch执行轨迹。
        fig,axes=plt.subplots(3,1,figsize=(16.5,9.5),sharex=True)
        xmax=max(T[dataset,a]['span_s'] for a in ARMS)
        for ax,arm in zip(axes,ARMS):
            tr=T[dataset,arm];order=sorted(range(12),key=lambda w:tr['first_model_by_worker'][str(w)])
            rank={w:i for i,w in enumerate(order)};span=tr['span_s']
            ax.axvspan(0,span*1000,color=STATES['idle'],zorder=-2)
            ax.axvspan(span*1000,xmax*1000,color='#EFEFEF',zorder=-2)
            for w in range(12):
                segs=[(a*1000,(b-a)*1000) for worker,a,b,_ in tr['red'] if worker==w]
                if segs:ax.broken_barh(segs,(rank[w]-.40,.8),facecolors=STATES['io'],edgecolors='none')
                for state in [*list(dataset),'mixed']:
                    segs=[(a*1000,(b-a)*1000) for worker,a,b,label in tr['comp'] if worker==w and label==state]
                    if segs:ax.broken_barh(segs,(rank[w]-.40,.8),facecolors=STATES[state],edgecolors='none')
            ax.set_yticks(range(12),order);ax.set_ylim(-.5,11.5);ax.invert_yaxis()
            ax.set_xlim(0,xmax*1000);ax.axvline(span*1000,color='#555555',ls='--',lw=.8)
            ax.set_ylabel('原实例号');ax.set_title(f'{LABEL[arm]}｜完成 {span*1000:.1f} ms',loc='left',fontsize=10)
            ax.grid(axis='x',alpha=.15)
        axes[-1].set_xlabel('仿真时间（ms）；每组按首次计算时间排序')
        fig.legend(handles=[Patch(color=STATES[k],label=k if k not in ('mixed','io','idle') else {'mixed':'混合计算','io':'未被模型计算覆盖的 IO','idle':'其他时间'}[k])
                   for k in [*list(dataset),'mixed','io','idle']],loc='upper center',ncol=7)
        fig.tight_layout(rect=(0,0,1,.95));save(fig,f'{dataset}_02_full-sort-ms')
        # 3. 模型计算占比，不能解释为物理NPU利用率。
        fig,(ax,heat)=plt.subplots(1,2,figsize=(14,4.8),gridspec_kw={'width_ratios':[1,1.5]})
        matrix=np.array([[float(next(r['model_pct'] for r in devices if r['dataset']==dataset and r['arm']==a and int(r['worker'])==w)) for w in range(12)] for a in ARMS])
        bp=ax.boxplot(matrix.T,patch_artist=True,showmeans=True)
        for box,a in zip(bp['boxes'],ARMS):box.set_facecolor(COL[a])
        ax.set_xticks([1,2,3],[LABEL[a] for a in ARMS]);ax.set_ylim(0,100);ax.set_ylabel('模型计算占比（%）');ax.grid(axis='y',alpha=.15)
        im=heat.imshow(matrix,aspect='auto',vmin=0,vmax=100,cmap='viridis')
        heat.set_yticks(range(3),[LABEL[a] for a in ARMS]);heat.set_xticks(range(12));heat.set_xlabel('原实例号')
        fig.colorbar(im,ax=heat,label='%');fig.suptitle(dataset+'｜纯模型计算 / 各自完成窗口')
        fig.tight_layout();save(fig,f'{dataset}_03_utilization')
        # 4. 带宽10ms积分平均；橙线为请求参考需求之和。
        fig,axes=plt.subplots(3,1,figsize=(14.6,8),sharex=True,sharey=True)
        bins=math.ceil(xmax/.01)
        ymax=90
        for ax,a in zip(axes,ARMS):
            actual=np.zeros(bins);demand=np.zeros(bins)
            for left,right,d,v in T[dataset,a]['band']:
                for i in range(max(0,int(left/.01)),min(bins,math.ceil(right/.01))):
                    overlap=max(0,min(right,(i+1)*.01)-max(left,i*.01))
                    actual[i]+=overlap*v/.01;demand[i]+=overlap*d/.01
            x=(np.arange(bins)+.5)*10
            ymax=max(ymax,float(demand.max()))
            ax.step(x,np.maximum(demand,.5),where='mid',lw=.8,color=style.BANDWIDTH_COLOR['demand'],label='申请参考需求')
            ax.step(x,np.maximum(actual,.5),where='mid',lw=.8,color=style.BANDWIDTH_COLOR['actual'],label='实际分配率')
            ax.axhline(90,ls='--',color='#222222',lw=.9,label='90 GB/s')
            ax.set_yscale('log');ax.set_ylabel('GB/s（对数）');ax.grid(alpha=.15)
            s=S[dataset,a];ax.set_title(f"{LABEL[a]}｜平均 {s['bandwidth_mean_GBps']:.1f} GB/s；超订 {s['over90_pct']:.1f}%；饱和 {s['saturated_pct']:.1f}%",loc='left',fontsize=10)
            bands.extend(dict(dataset=dataset,arm=a,mid_ms=float(t),demand_GBps=float(d),actual_GBps=float(v)) for t,d,v in zip(x,demand,actual))
        axes[0].set_ylim(.5,ymax*1.25)
        axes[0].legend(loc='upper right',ncol=3);axes[-1].set_xlabel('仿真时间（ms）；零值仅在图中显示为 0.5 GB/s');fig.tight_layout();save(fig,f'{dataset}_04_bandwidth')
        # 5. 总体CDF及逐类CDF。
        fig,ax=plt.subplots(figsize=(10.2,5.4))
        for a in ARMS:
            rr=[r for r in cdf if r['dataset']==dataset and r['arm']==a and r['class_id']=='all']
            ax.step([0]+[r['ttft_ms'] for r in rr],[0]+[r['cdf'] for r in rr],where='post',color=COL[a],label=LABEL[a])
        ax.set_ylim(0,1.01);ax.set_xlabel('TTFT（ms）');ax.set_ylabel('累计概率');ax.legend();ax.grid(alpha=.15);ax.set_title(dataset+'｜总体 TTFT')
        fig.tight_layout();save(fig,f'{dataset}_05_cdf')
        fig,axes=plt.subplots(1,len(dataset),figsize=(16,4.3),sharey=True)
        for ax,label in zip(axes,list(dataset)):
            for a in ARMS:
                rr=[r for r in cdf if r['dataset']==dataset and r['arm']==a and r['class_id']==label]
                ax.step([0]+[r['ttft_ms'] for r in rr],[0]+[r['cdf'] for r in rr],where='post',color=COL[a],label=LABEL[a])
            ax.set_title(label);ax.set_xlabel('TTFT（ms）');ax.grid(alpha=.15);ax.set_ylim(0,1.01)
        axes[0].set_ylabel('累计概率');axes[-1].legend(fontsize=8);fig.tight_layout();save(fig,f'{dataset}_06_cdf_by_class')
    csvout('bandwidth_10ms.csv',bands)
    text='# 全局4%计算额度：90 GB/s下的直接准入与新TFS\n\n'
    text+=table(['项目','约定'],[
        ['日期','2026-10-29'],['输入','Mooncake conversation前360条，保留input和到达节奏；不使用原output和hash推导命中'],
        ['构造','ABC=240/90/30；ABCD=220/90/30/20；新计算总204699 token，约3.999781%'],
        ['存储','单共享池90 GB/s，Grouped max-min，零启动延迟；每组读取539571462144 B'],
        ['资源','12个真实vLLM实例与原生Dynamo；GLM5实际组批v2；TP1/DP8/EP8；78层流水'],
        ['调度','各组均EDF；batch≤4，8192新token/步，block16，K=1'],
        ['预热与重复','每组72预热排空、重置历史和正式GC；每方案一次；六组固定seed打乱串行'],
        ['计时','网关实际到达→首token；SLO逐请求TTFT≤4×新T0'],
        ['源码','当前DV工作树统一快照，包含未提交改动；精确文件SHA见provenance'],
        ['边界','仅模拟模型执行和共享存储；不是Ascend实测；不运行长期decode']])
    text+='\n## 0. 总判决\n\n'
    for dataset in ['ABC','ABCD']:
        rows=[S[dataset,a] for a in ARMS]
        best_mean=min(rows,key=lambda r:r['mean_ttft_ms']);best_end=min(rows,key=lambda r:r['completion_ms']);best_slo=max(rows,key=lambda r:r['slo_pct'])
        text+=f"{dataset}：本次平均TTFT最低为 **{LABEL[best_mean['arm']]}**；完成最快为 **{LABEL[best_end['arm']]}**；SLO最高为 **{LABEL[best_slo['arm']]}**。每方案仅一次，不判断稳定最优。\n\n"
    text+='## 1. 汇总表与SLO\n\n'
    text+=table(['负载/方案','完成ms','平均TTFT ms','p95 ms','SLO %','平均batch'],[
        [r['dataset']+'/'+LABEL[r['arm']],*[f'{r[k]:.1f}' for k in ['completion_ms','mean_ttft_ms','p95_ms','slo_pct']],f"{r['mean_batch']:.3f}"] for r in summary])
    text+='\n直接准入立即转发且完整计分；两TFS在Scheduler分配slot前申请到达冻结P75配额，首token释放，无强制准入。两TFS仅waiting_factor不同。三组共用同步batch反馈与compact trace。\n'
    for dataset in ['ABC','ABCD']:text+=img(dataset+'_01_slo','按同一请求的新T0判断达标；类别为人工构造标签，不等同在线重读分类。')
    text+='## 2. 完整执行轨迹与实际组批\n\n'
    for dataset in ['ABC','ABCD']:
        text+=img(dataset+'_02_full-sort-ms','每组12行，各自按首次计算排序。A蓝、B橙、C绿、D棕、混合紫；红为在途IO未覆盖模型计算，灰为其他时间，虚线后为补齐。')
        text+=table(['方案','step数','batch1/2/3/4','跨step请求数'],[[LABEL[a],S[dataset,a]['steps'],'/'.join(str(S[dataset,a][f'batch{i}']) for i in range(1,5)),S[dataset,a]['chunked_requests']] for a in ARMS])+'\n'
    text+='## 3. 模型计算占比\n\n'
    for dataset in ['ABC','ABCD']:text+=img(dataset+'_03_utilization','每实例纯模型计算时间/本组实际完成窗口；不含IO、控制和sleep超时，不是硬件GPU/NPU利用率。')
    text+=table(['负载/方案','模型计算总秒','平均模型计算占比%','实例计算量CV'],[[r['dataset']+'/'+LABEL[r['arm']],f"{r['model_s']:.3f}",f"{r['model_pct']:.2f}",f"{r['worker_model_cv']:.3f}"] for r in summary])
    text+='\nCV为12个实例模型计算量的总体标准差/均值。CV较低表示计算量较均衡。完成窗口变短也会提高计算占比，不能只用计算占比判断节省了多少计算。\n\n'
    text+='## 4. 存储带宽时序\n\n'
    for dataset in ['ABC','ABCD']:text+=img(dataset+'_04_bandwidth','10ms积分平均，同一负载三图共用对数纵轴。零值仅在图中显示为0.5，CSV保留零值。橙线累计已提交读取的请求参考需求，未按真实batch重新估算；蓝线为分配率，虚线90GB/s。')
    text+=table(['负载/方案','平均GB/s','需求超订%','饱和%','无在途IO秒'],[[r['dataset']+'/'+LABEL[r['arm']],f"{r['bandwidth_mean_GBps']:.1f}",f"{r['over90_pct']:.1f}",f"{r['saturated_pct']:.1f}",f"{r['no_io_s']:.3f}"] for r in summary])
    text+='\n同字节量在90GB/s下至少需要5.995238秒满带宽传输。当前工作守恒模型只要有读取就用满池带宽，无IO时段可包含计算、准入等待和控制，不能全部归为可消除损失。\n\n'
    text+='## 5. 首token延迟CDF\n\n'
    for dataset in ['ABC','ABCD']:
        text+=img(dataset+'_05_cdf','rank/n阶梯经验CDF，终点1；p95用nearest-rank。')
        text+=img(dataset+'_06_cdf_by_class','固定人工类别成员；A/B/C/D在同一负载三方案中一致，便于检查收益和代价由哪些请求承担。')
    text+='## 6. 机制解释与受控比较\n\n'
    for dataset in ['ABC','ABCD']:
        base=S[dataset,'direct']
        for a in ARMS[1:]:
            r=S[dataset,a]
            text+=f"{dataset} {LABEL[a]}相对直接准入：平均TTFT变化{(r['mean_ttft_ms']/base['mean_ttft_ms']-1)*100:+.2f}%，完成时间变化{(r['completion_ms']/base['completion_ms']-1)*100:+.2f}%，SLO变化{r['slo_pct']-base['slo_pct']:+.2f}个百分点。正的时间变化表示变慢。\n\n"
    text+='同一负载内T0/期限一致。ABC到ABCD把最长20条A改成D，并从B调出计算额度，保持总新token/总读取量不变，但计算总时间仍可因上下文与组批变化而不同。不能把新负载与原96% case跨版本差值称为TFS优化收益。\n\n'
    text+='ABC的α=0.1主要改善B/C：平均TTFT分别由2407.1/1862.9ms降到964.9/770.8ms；A只由1362.3ms降到1313.3ms。ABCD的α=1主要改善B/C/D，A平均TTFT反而由1140.8ms增至1253.2ms。各请求期限不同，因此A的平均TTFT变差可以与A的SLO达标率提高同时发生。\n\n'
    text+='这些收益没有依赖更大的batch。ABC的α=0.1与ABCD的α=1都减小了batch，同时增加了模型计算总时间。ABCD的α=1把平均带宽从60.8提高到78.6GB/s，无在途IO时间从2.885缩短到0.873秒，实例计算量CV也从0.233降到0.081。这些现象支持读取与计算衔接改善的解释，但单次运行不能确定唯一原因。\n\n'
    text+='ABCD的α=0.1是反例：全部完成更快，平均TTFT、p95和SLO却变差。减少尾部时间不保证每个请求更快，也不能把α=0.1当作通用最优值。\n\n'
    text+='## 7. 限制\n\n每方案一次。K=1包含HTTP、IPC、轮询和系统调度开销；全部运行使用电池供电，主机有既存swap，电源/内存记录见matrix.progress。GLM公式未用Ascend910C实机校准，采用均匀专家路由假设。缓存命中人为分配、文本合成、本地前缀缓存关闭，仅prefill-only与输出1 token。\n\n'
    text+='## 8. 分类附表\n\n'
    for dataset in ['ABC','ABCD']:
        text+=f'### {dataset}\n\n'+table(['方案/类别','数量','平均TTFT ms','p95 ms','SLO %'],[[LABEL[r['arm']]+'/'+r['class_id'],r['n'],f"{r['mean_ttft_ms']:.1f}",f"{r['p95_ms']:.1f}",f"{r['slo_pct']:.1f}"] for r in classes[dataset] if r['class_id']!='all'])+'\n'
    text+='## 9. 验证与复现\n\n六组各360正式+72预热成功；另三次冒烟6/6/8条。原生路由/完整资源预留、预算token、字节、公式、逐层屏障、配额归零、GC恢复及进程回收由运行验收与独立提取共同检查。报告检查2160请求CDF、72份时间账本与带宽积分。\n\n'
    text+='[逐运行表](report_data/run_summary.csv)、[逐请求表](report_data/requests.csv)、[分类表](report_data/class_summary.csv)、[带宽桶](report_data/bandwidth_10ms.csv)、[提取核对](report_data/extraction_checks.json)、[来源指纹](report_data/provenance.json)、[报告验证](report_data/validation.json)、[复现说明](reproduce/README.md)。\n\n运行build_report.py可从紧凑数据重画。分享包包含本次小型运行源码快照与构造表，不含虚拟环境、原生二进制和完整原始事件。完整原始事件保留本机DV results/mooncake360_global4_90g_20261029。\n\n'
    text+='## 10. 阅读顺序\n\n先看第1节总体结果，再用第8节定位受益和受损类别，结合第2/4节检查组批、争抢与尾部。需要核实数值时查看report_data与本机原始记录。\n'
    (OUT/TITLE).write_text(text)
    if HERE!=OUT:shutil.copy2(__file__,OUT/'build_report.py')
    print('report and 12 figures generated',flush=True)


if __name__=='__main__':
    main()
