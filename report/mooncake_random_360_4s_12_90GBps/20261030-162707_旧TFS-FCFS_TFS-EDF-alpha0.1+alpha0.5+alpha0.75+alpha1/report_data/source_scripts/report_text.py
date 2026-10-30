"""Chinese report text; all figures and values derive from frozen run records."""
def render(cases, inp, provenance, checks, table, image, label, modes, read, data):
    metrics = {m: cases[m]['metrics'] for m in modes}
    alphas = ['a01', 'a05', 'a075', 'a1']
    best = min(alphas, key=lambda m: metrics[m]['completion_ms'])
    best_slo = max(alphas, key=lambda m: metrics[m]['slo_pct'])
    s = '# 实验结果：Mooncake360 90 GB/s计分阈值对照\n\n'
    s += table(['项目', '约定'], [
        ['实验日 / 整理日', '2026-10-29；新跑α0.5、0.75、1各一次；复用此前90GB/s的α0.1和旧TFS'],
        ['输入', 'conversation前360条，保留顺序和同刻组；计划0–4秒到达；A90/B270为固定展示标签'],
        ['命中', '人为96%远端前缀命中，向下对齐完整16-token块；实际token命中率95.9446%'],
        ['工作量', f"{inp['input_tokens']}输入token；{inp['hit_tokens']}命中；{inp['new_tokens']}新增token；{inp['total_read_bytes']}B读取"],
        ['资源', '1个真实Dynamo前端、12个真实vLLM实例及EngineCore；设备计算与传输为仿真；K=1'],
        ['组批', 'batch≤4；每步8192新增token；30159逻辑块；block16；本地前缀缓存关闭'],
        ['计算', 'glm5_direct_sum_v1；launch_saving_fraction=1；78层；固定参考TP1/DP8/EP8'],
        ['存储与额度', '90 GB/s统一池；逐层读、预取下一层；P75人数额度；首token释放；强制准入关闭'],
        ['受控变量', '四组新TFS仅routing_accounting.waiting_factor不同；α1仍保留准入RPC、额度和记账'],
        ['时间线分类', '蓝色重读、橙色轻读、紫色轻重混批；新TFS用到达时冻结标记；旧TFS取每层开始时动态P75快照'],
        ['预热 / 重复', '每组72条预热并排空、清画像历史、重置GC；360条正式；每配置只有一次'],
        ['完成 / TTFT', '完成=首条正式网关到达→最后首token；TTFT=网关登记→网关首token，包含所有等待'],
        ['SLO', '逐请求TTFT≤4T0；glm5_reference_v3；共同期限；nearest-rank分位数'],
        ['通信', 'Dynamo Runtime TCP/MessagePack；四种在线执行反馈已移除；读取事实仅离线合并'],
        ['源码', f"沿用旧90GB/s冻结DV {provenance['source_commit']}及原有诊断补丁；运行文件与native逐项一致"],
        ['输入SHA256', inp['effective_trace_sha256']],
    ])+'\n'
    s += '## 0. 总判决\n\n'
    s += f"四个α中，本次完成最快的是 **{label[best]}：{metrics[best]['completion_ms']:.1f} ms**；SLO最高的是 **{label[best_slo]}：{metrics[best_slo]['slo_pct']:.1f}%**。每配置只执行一次；这里只说明实际运行结果，不证明稳定最优阈值。\n\n"
    s += table(['方案', '完成ms', '平均TTFT ms', 'p95 ms', 'SLO %', '数据来源'], [
        [label[m], *[f"{metrics[m][k]:.2f}" for k in ['completion_ms','mean_ttft_ms','p95_ms','slo_pct']], '复用' if m in ('old-tfs','a01') else '本次新跑'] for m in modes])+'\n'
    for m in ('a05','a075','a1'):
        v,b=metrics[m],metrics['a01']
        s += f"{label[m]}相对α0.1：完成时间 **{(v['completion_ms']/b['completion_ms']-1)*100:+.2f}%**，平均TTFT **{(v['mean_ttft_ms']/b['mean_ttft_ms']-1)*100:+.2f}%**，SLO **{v['slo_pct']-b['slo_pct']:+.2f}个百分点**。\n\n"
    s += '时间变化为正表示变慢。本次新增1080条正式请求，三组均完成；对照表共1800条，另外720条来自此前两组。不能把复用组称作同期重跑。\n\n'
    s += '## 1. 汇总表与 SLO\n'
    s += image('01_slo_bar', '每配置一次，不含预热。柱高为总体SLO；A/B为历史展示分组的独立满足率，不是在线轻重，不作堆叠。旧TFS与α0.1复用。')
    s += table(['方案','准入位置','实例排序','等待重读计分','历史A SLO %','历史B SLO %'], [
        [label[m], '网关' if m=='old-tfs' else '调度前', 'FCFS' if m=='old-tfs' else 'EDF', '完整' if m=='old-tfs' else str(cases[m]['config']['routing_accounting']['waiting_factor']), f"{metrics[m]['A_slo_pct']:.2f}", f"{metrics[m]['B_slo_pct']:.2f}"] for m in modes])+'\n'
    s += 'α1仍然是新TFS。它只取消等待负载的评分折扣，不取消P75额度，不改为直接放行。旧TFS与新TFS还改变了准入位置、分类冻结和实例排序，因此旧TFS只作参照。\n\n'
    s += '## 2. 完整执行时间线与组批\n'
    s += image('02_full-sort-ms', '按控制器实际轻重判定着色：蓝色重读、橙色轻读、紫色轻重混批。新TFS使用到达时冻结标记；旧TFS使用每层计算开始时动态P75，分类变化会改色。红色是未被计算覆盖的在途IO，灰色含空闲、排队和控制。虚线后仅补齐横轴。')
    s += '**本图不再用历史A/B着色。** 本次只离线重画，全部请求、batch、计算时长、TTFT和SLO保持原值。这里的轻重是策略按画像估计作出的实际判定，不是对真实存储压力的硬件测量。\n\n'
    s += table(['方案','准入时重读数','准入时轻读数','计算期间曾重读数','计算期间曾轻读数','计算中变类数'],[
        [label[m],*[cases[m]['online'][key] for key in ['admission_heavy','admission_light','compute_heavy_any','compute_light_any','changed_requests']]] for m in modes])+'\n'
    s += '新TFS的冻结标记不随阈值变化；旧TFS的持有额度计数会随动态阈值重新分类。表中的“曾重读”和“曾轻读”可能重叠，因此两列之和不要求为360。每层颜色取层开始时快照，不表示层内连续追踪；紫色只表示该层batch同时包含轻读与重读。\n\n'
    s += table(['方案','step数','平均batch','batch 1/2/3/4','跨step请求数'], [
        [label[m],metrics[m]['steps'],f"{metrics[m]['mean_batch_size']:.3f}",'/'.join(str(cases[m]['batch'].get(n,0)) for n in range(1,5)),metrics[m]['split_requests']] for m in modes])+'\n'
    s += '直接组合模式只减少重复算子启动项。batch更大不等于计算成本大幅下降；请求组合和实例承担的计算量仍会变化。\n\n'
    s += '## 3. 模型计算占比、红色IO与负载分布\n'
    s += image('03_util_box_heatmap', '每实例名义模型计算量/本组完成窗口；箱线与热力图使用相同12个值，色标0–100%。这不是硬件实测利用率。')
    s += table(['方案','计算实例秒','计算占比%','红色实例秒','红色占比%','灰色实例秒','最大实例计算秒','计算量CV'], [
        [label[m],*[f"{metrics[m][k]:.3f}" for k in ['model_compute_s','model_compute_pct','red_instance_s','red_pct','gray_instance_s','max_worker_model_s','worker_model_cv']]] for m in modes])+'\n'
    s += '实例秒是12个实例分别计时后的累计值，不能直接减去全局完成时间。红色按同实例IO区间取并集，再扣除名义模型计算区间；它不等于纯IO依赖阻塞或纯争抢损失。灰色也不能全部视为可删除开销。CV为12个实例模型计算量的总体标准差/均值，越小表示计算量越均衡。\n\n'
    s += '## 4. 存储带宽时序\n'
    s += image('04_bandwidth', '需求橙线、实际速率蓝线、容量90GB/s黑虚线。逐层物理读取区间按10ms精确积分；对数底线0.5只显示零值，CSV保留0。')
    s += table(['方案','平均GB/s','需求超订%','实际饱和%'],[[label[m],*[f"{metrics[m][k]:.2f}" for k in ['mean_bandwidth_GBps','demand_oversubscribed_pct','saturated_pct']]] for m in modes])+'\n'
    s += '每组读取539.259GB，持续占满90GB/s至少需要5.992秒。需求超订按需求>B，实际饱和按分配速率≥0.9B。统一池中更改路由不会增加物理带宽；读取时序、并发和计算重叠决定是否出现带宽空档。\n\n'
    s += '## 5. TTFT 累积分布\n'
    s += image('05_cdf', '网关TTFT包含所有等待；经验CDF为rank/n，终点1。逐请求期限不同，SLO按请求匹配计算，不能由CDF交点推导。')
    s += image('06_cdf_by_class', '本图保留历史A90/B270，供原对照统计使用；它不是图2的在线轻重分类。两类均可含远端读取；不画虚构的统一期限线。')
    s += table(['方案','p50 ms','p95 ms','A均值ms','B均值ms'],[[label[m],*[f"{metrics[m][k]:.2f}" for k in ['p50_ms','p95_ms','A_mean_ms','B_mean_ms']]] for m in modes])+'\n'
    s += '## 6. 机制解释与准入开销\n\n'
    s += table(['方案','网关等待ms','转发→首次读取ms','正式acquire次数','拒绝次数','单次均值ms','单次p95 ms','末尾实例RPC累计ms'],[
        [label[m],f"{metrics[m]['mean_gateway_wait_ms']:.2f}",f"{metrics[m]['mean_submitted_to_read_ms']:.2f}",cases[m]['rpc']['calls'],cases[m]['rpc']['denied'],f"{cases[m]['rpc']['mean_ms']:.3f}",f"{cases[m]['rpc']['p95_ms']:.3f}",f"{metrics[m]['tail_rpc_s']*1000:.2f}"] for m in modes])+'\n'
    s += 'RPC仅统计360条正式请求的同步acquire，排除预热；调用耗时包含客户端桥接、中央排队、获准后的评分激活和返回，不能称作纯网络RTT。旧网关TFS没有这条Scheduler acquire路径，0不代表没有控制成本。跨实例累计RPC时间不能直接从整轮时间扣除。\n\n'
    s += 'α越大，未获准重请求在路由分数中保留的等待负载越多；原生完整资源booking、picker和guard始终保留。请求获准后补齐分数。改变α会改变实例收到的请求和batch，最终差异不能简化成RPC耗时差。是否更均衡应结合最大实例计算量与CV，是否更快应结合完成时间，是否满足期限应单独看SLO。\n\n'
    s += '四组新TFS的静态配置除α之外完全相同。但原生同分选择和宿主时序仍有波动；同一trace也可能得到略不同的冻结重读集合。本次不根据单轮差值给出稳定因果比例。\n\n'
    b,v=metrics['a01'],metrics['a075']
    s += f"本次α0.75相对α0.1，最大实例模型计算量从{b['max_worker_model_s']:.3f}降到{v['max_worker_model_s']:.3f}秒，灰色累计从{b['gray_instance_s']:.3f}降到{v['gray_instance_s']:.3f}实例秒。与此同时，正式acquire次数从{cases['a01']['rpc']['calls']}增加到{cases['a075']['rpc']['calls']}，末尾实例累计RPC从{b['tail_rpc_s']*1000:.1f}增加到{v['tail_rpc_s']*1000:.1f}ms，整轮却更快。这个轨迹反例说明，不能只用RPC次数或其直接耗时解释最终完成时间。\n\n"
    o=metrics['old-tfs']
    s += f"α0.75本次比旧TFS完成时间变化{(v['completion_ms']/o['completion_ms']-1)*100:+.2f}%，但平均TTFT变化{(v['mean_ttft_ms']/o['mean_ttft_ms']-1)*100:+.2f}%，SLO变化{v['slo_pct']-o['slo_pct']:+.2f}个百分点。整体排空更早不代表每条请求更早完成。\n\n"
    s += '## 7. 计算模型与结论限制\n\n'
    s += '- 单请求模型时间逐层相加，扣除max(n−1,0)×3×7µs×78的显式重复启动；不额外摊薄权重或专家通信。目标硬件绝对时延未定标。\n'
    s += '- 每配置仅一次。α0.1与旧TFS复用此前结果，三组新增依次执行；没有重复试验或置信区间。不能保证排名稳定。\n'
    s += '- 真实Dynamo/vLLM控制面，CPU模拟模型执行与存储。仿真比例不是硬件利用率，回环RPC不代表跨机网络。\n'
    s += '- 只做prefill、每请求输出1 token；96%命中是实验假设，不是原始trace的实测缓存命中率。\n\n'
    s += '## 8. 附表：投递与资源\n'
    s += image('07_arrival_lateness', '实际客户端发送减计划发送。计划完全相同不保证实际到达一致；主TTFT从网关登记开始，客户端口径另存。')
    s += table(['方案','投递迟到p95 ms','整轮壁钟s','RSS峰值GiB','最低可用GiB','swap峰值GiB'],[
        [label[m],f"{metrics[m]['lateness_p95_ms']:.2f}",f"{read(data/'raw'/m/'driver.json')['wall_s']:.2f}",
         f"{read(data/'raw'/m/'driver.json')['peak_children_rss_gib']:.3f}",
         f"{read(data/'raw'/m/'driver.json')['min_available_gib']:.3f}",
         f"{read(data/'raw'/m/'driver.json')['max_swap_gib']:.3f}"] for m in modes])+'\n'
    s += '资源保护和清理结果按各组driver.json保留；正式窗口没有自动调整策略或重放模糊RPC结果。\n\n'
    s += '## 9. 验证、数据与复现\n\n'
    s += '本次新增三组各72预热＋360正式请求，共1080正式；两组历史对照保持原始记录。输入SHA、运行文件指纹、native、仅α配置差异、token/字节、78层屏障、EDF、公式、配额/评分排空、预热/GC恢复与进程清理均检查。离线独立复核1800条请求、60份实例账本、CDF、10ms带宽积分与原两组汇总一致性；图表目检和ZIP检查见验证清单。\n\n'
    s += '- [逐运行统计](report_data/run_summary.csv)、[逐请求](report_data/requests.csv)、[实例账本](report_data/device_utilization.csv)、[实例计算量及RPC](report_data/workers.csv)。\n'
    s += '- [正式RPC统计](report_data/formal_rpc.json)、[准入统计](report_data/admission_summary.csv)、[带宽桶](report_data/bandwidth_10ms.csv)、[CDF](report_data/cdf.csv)。\n'
    s += '- [输入与版本](report_data/input_check.json)、[来源](report_data/provenance.json)、[运行收据](report_data/matrix_receipt.json)、[验证](report_data/validation.json)。\n'
    s += '- [逐请求在线分类](report_data/online_classification_requests.csv)、[逐层在线分类](report_data/online_classification_layers.csv)、[分类汇总](report_data/online_classification_summary.csv)、[分类实现](online_classification.py)。\n'
    s += '- [收集](collect_report.py)、[重画与汇总](build_report.py)、[独立核对](verify_report.py)、[样式](report_style.py)、[依赖](requirements-report.txt)、[实验驱动](reproduce/runner.py)。\n\n'
    s += '完整冻结Linux日志目录记录在provenance.json，reproduce包含配置包装及旧控制编排。重画报告使用本目录raw投影，不启动服务；重跑实验还需要固定Linux环境和native扩展。完整ZIP留本机，不自动公开原始数据。\n\n'
    s += '## 10. 简要解读\n\n'
    s += f"先比较四个α：本次完成时间最短为{label[best]}，SLO最高为{label[best_slo]}。再与旧TFS比较，分别检查完成、平均TTFT和SLO。随后查看最大实例计算量、红色IO、灰色时间与同步RPC，避免把某一个指标改善当作全部收益。以上是单次结果，不据此更改默认策略。\n"
    return s
