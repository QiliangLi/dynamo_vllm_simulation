"""中文报告正文；所有性能数字来自本轮冻结记录。"""


def render(cases, inp, provenance, checks, table, image, label, modes, read, data):
    metrics = {m: cases[m]['metrics'] for m in modes}
    base, new, forced = (metrics[m] for m in ('fcfs', 'new-tfs', 'idle-bypass'))
    fastest = min(modes, key=lambda m: metrics[m]['completion_ms'])
    highest = max(modes, key=lambda m: metrics[m]['slo_pct'])
    s = '# 实验结果：Mooncake360 90 GB/s＋直接组合计时五方案对照\n\n'
    s += table(['项目', '约定'], [
        ['实验日 / 整理日', '2026-10-29；五组均重新运行'],
        ['输入', 'conversation 前360条，保留顺序和同刻组，计划到达窗口0–4秒'],
        ['命中', '人为96%远端命中，向下对齐完整16-token块；本地前缀缓存关闭'],
        ['输入总量', f"{inp['input_tokens']:,}输入token；{inp['hit_tokens']:,}命中；{inp['new_tokens']:,}新计算"],
        ['资源', '1个Dynamo前端、12个真实vLLM逻辑实例及EngineCore、独立准入服务；K1'],
        ['组批', 'max_num_seqs=4；max_num_batched_tokens=8192；逻辑块池30159块'],
        ['计算', 'glm5_direct_sum_v1；单请求时间相加，仅扣显式重复算子启动；合并比例1；固定TP1/DP8/EP8；78层'],
        ['存储', '90 GB/s单共享池、GroupedStorage max-min、零启动延迟；逐层读、预取下一层'],
        ['方案', '直接FCFS、直接EDF、旧网关TFS、新调度前TFS α=0.1、新TFS＋空闲强制准入'],
        ['预热 / 重复', '每组72条预热，排空后清画像历史并重置GC窗口；正式360条；每方案1次'],
        ['完成与吞吐', '首条正式请求网关登记→最后一条正式首token；请求吞吐=360/该窗口'],
        ['TTFT / SLO', 'TTFT=网关首token−登记时间，含全部等待；TTFT≤4T0；T0按最新画像v3统一计算'],
        ['统计', 'p50/p95采用nearest-rank；每条期限分别匹配；无置信区间'],
        ['通信', 'Dynamo Runtime TCP/MessagePack准入；四种在线执行反馈已移除；首token释放保留'],
        ['观测', '各组相同本地细粒度记录；正式窗口网关和存储循环GC暂停，结束后恢复'],
        ['展示分组', 'SLO与CDF保留历史A90/B270；时间线按在线轻重重绘；无C'],
        ['源码', f"DV {provenance['source_commit']}，另保留原有RPC阶段诊断补丁；五组同源码和native扩展"],
        ['输入SHA256', inp['effective_trace_sha256']],
        ['执行边界', '真实控制面和Scheduler；GPU计算、专家通信与远端存储传输由模型模拟'],
    ]) + '\n'
    s += '## 0. 总判决\n\n'
    s += f"本轮完成最快的是 **{label[fastest]}：{metrics[fastest]['completion_ms']:.1f} ms**。总体SLO最高的是 **{label[highest]}：{metrics[highest]['slo_pct']:.1f}%**。每方案只有一次，结果只描述本轮，不证明稳定排名。\n\n"
    s += table(['方案', '完成ms', '请求/s', '平均TTFT ms', 'p95 ms', 'SLO %'], [
        [label[m], *[f'{metrics[m][k]:.2f}' for k in ['completion_ms', 'requests_per_s', 'mean_ttft_ms', 'p95_ms', 'slo_pct']]] for m in modes]) + '\n'
    for name, a, b in [('新TFS相对直接FCFS', new, base), ('新TFS相对旧TFS', new, metrics['old-tfs']), ('强制准入相对新TFS', forced, new)]:
        s += f"{name}：完成时间变化 **{(a['completion_ms']/b['completion_ms']-1)*100:+.2f}%**，平均TTFT变化 **{(a['mean_ttft_ms']/b['mean_ttft_ms']-1)*100:+.2f}%**，SLO变化 **{a['slo_pct']-b['slo_pct']:+.2f}个百分点**。时间变化为正表示变慢。\n\n"
    s += '五组共1800条正式请求全部返回首token并正常结束。全部结果采用本次重新运行的数据。\n\n'
    s += '## 1. 汇总表与 SLO\n'
    s += image('01_slo_bar', '柱高为总体SLO；横轴附A/B各自SLO。每组样本相同，不含72条预热；没有把分类SLO相加。')
    s += table(['方案', '准入', '实例内排序', '等待重读计分', '强制'], [
        ['直接FCFS', '网关立即转发', 'FCFS', '完整', '关'],
        ['直接EDF', '网关立即转发', '固定deadline EDF', '完整', '关'],
        ['旧TFS', '网关配额控制，动态P75', 'FCFS', '完整', '关'],
        ['新TFS α=0.1', '实例调度前申请，分类到达时冻结', 'EDF', '等待0.1，获准补齐', '关'],
        ['新TFS＋强制', '同新TFS', 'EDF', '同新TFS', '满足条件的空闲实例每轮至多1条；不等上一笔额度释放'],
    ]) + '\n'
    s += table(['方案', 'A平均TTFT ms', 'B平均TTFT ms', 'A SLO %', 'B SLO %'], [
        [label[m], *[f'{metrics[m][k]:.2f}' for k in ['A_mean_ms', 'B_mean_ms', 'A_slo_pct', 'B_slo_pct']]] for m in modes]) + '\n'
    s += f"共同的期限宽度范围为 **{base['deadline_min_ms']:.2f}–{base['deadline_max_ms']:.2f} ms**，均值 **{base['deadline_mean_ms']:.2f} ms**。同一请求跨五组使用相同期限宽度。\n\n"
    s += '当前T0按单请求和8192-token参考分块计算，并按逐层预取协议合成读取与计算重叠。这与早期直接将整段读取加计算的画像不同。SLO既不是trace自带要求，也不是固定毫秒数；倍率4为实验设定。\n\n'
    s += '## 2. 完整执行时间线与组批\n'
    s += image('02_full-sort-ms', '五组各12行，按在线判定绘制：蓝色重读、橙色轻读、紫色轻重混批。新TFS使用到达时冻结标记；旧TFS及直接放行取每层开始时的动态阈值，直接放行的分类仅作参考、不代表额度限制。红色为未被名义计算覆盖的在途IO；灰色含空闲、排队和控制。虚线后仅补齐横轴。')
    s += '本图使用固定重绘脚本从原始日志生成，不再按历史A/B着色；本次没有重跑实验，全部性能统计保持原值。其他SLO和分类CDF表中的A/B仍是历史展示分组。\n\n'
    s += table(['方案', 'step数', '多请求step', '平均批大小', '批大小1/2/3/4', '跨step请求数'], [
        [label[m], metrics[m]['steps'], metrics[m]['multi_request_steps'], f"{metrics[m]['mean_batch_size']:.3f}",
         '/'.join(str(cases[m]['batch'].get(i, 0)) for i in range(1, 5)), metrics[m]['split_requests']] for m in modes]) + '\n'
    s += '平均批大小=各step请求成员次数之和/step数。请求分块后可以进入多个step，因此不能总用360/step数代替。所有实际step的新token合计必须等于207547，批大小不能用层色块数推断。\n\n'
    s += table(['方案', '各step单请求模型时间之和：实例秒', '合并启动减少：实例毫秒', '实际模型计算：实例秒'], [
        [label[m], f"{metrics[m]['singleton_model_s']:.6f}", f"{metrics[m]['saved_launch_ms']:.3f}", f"{metrics[m]['model_compute_s']:.6f}"] for m in modes]) + '\n'
    s += '这里的启动减少量是12实例累计值，不能直接当作整批完成时间的缩短量。新模式只减少显式重复启动，合批不会再摊薄权重读取或专家通信时间。\n\n'
    s += '## 3. 模型计算占比分布与热力图\n'
    s += image('03_util_box_heatmap', '每实例名义模型计算时间/本方案完成窗口。箱线和热图使用同一组12个值，色标0–100%。它不是宿主CPU利用率或真实GPU利用率；观察到的计算段还包含sleep超时和本地程序开销。')
    s += table(['方案', '模型计算实例秒', '4秒窗口计算压力%', '全程模型计算占比%'], [
        [label[m], f"{metrics[m]['model_compute_s']:.3f}", f"{metrics[m]['compute_pressure_pct']:.2f}", f"{metrics[m]['model_compute_pct']:.2f}"] for m in modes]) + '\n'
    s += f"输入侧单请求参考计算总量为{inp['reference_compute_instance_s']:.3f}实例秒，除以12×4秒为{inp['offered_reference_compute_load']*100:.2f}%。上表采用各组实际batch的估时，两种计算压力口径分开。\n\n"
    s += '## 4. 存储带宽时序\n'
    s += image('04_bandwidth', '橙线为需求，蓝线为模型实际分配速率，黑虚线为90GB/s。由逐请求逐层读取端点重建区间，再按10ms桶积分；零值只在对数图中显示为0.5，CSV保留0。')
    s += table(['方案', '平均GB/s', '需求超订%', '实际饱和%', '积分误差B'], [
        [label[m], f"{metrics[m]['mean_bandwidth_GBps']:.2f}", f"{metrics[m]['demand_oversubscribed_pct']:.2f}", f"{metrics[m]['saturated_pct']:.2f}", f"{checks[m]['bandwidth_error_bytes']:.6f}"] for m in modes]) + '\n'
    s += f"各组正式读取均为 **{inp['total_read_bytes']:,} B（{inp['total_read_bytes']/1e9:.3f} GB）**，连续占满90GB/s也需要{inp['total_read_bytes']/90e9:.3f}秒。4秒输入窗口的存储压力为{inp['offered_storage_load']*100:.2f}%。需求超订按需求>B，饱和按实际≥0.9B，分母为各组完成窗口。\n\n"
    s += '重建使用本轮单盘、零启动延迟、始终分配剩余带宽的GroupedStorage语义。端点为双精度时间，末尾按剩余字节裁剪会留下少量积分误差。预先沿用1e-11相对误差阈值；没有缩放速率或改写原时间来消除误差。\n\n'
    s += '## 5. TTFT 累积分布\n'
    s += image('05_cdf', '每条曲线360条，按rank/n阶梯绘制。点线为逐请求期限宽度分布。期限各不相同，SLO按每个请求匹配计算，不能由两条CDF交点推断。')
    s += image('06_cdf_by_class', '固定A90/B270展示标签。分类与在线重读判定分开，不把A固定解释为当轮全部重请求。分类期限也不统一。')
    s += table(['方案', 'p50 ms', 'p95 ms', '客户端平均TTFT的统计范围'], [
        [label[m], f"{metrics[m]['p50_ms']:.2f}", f"{metrics[m]['p95_ms']:.2f}", '客户端另存逐请求表；主表使用网关口径'] for m in modes]) + '\n'
    s += '## 6. 机制解释与同条件比较\n\n'
    s += '直接FCFS与直接EDF只改变policy/request_ordering。新TFS与强制准入只改变admission.idle_bypass。旧TFS与新TFS同时改变准入位置、分类时刻、实例排序和等待计分，不能将全部差异归因于α。\n\n'
    s += table(['方案', '网关等待均值ms', '转发到首个读取均值ms', '重读标记数', '正式拒绝次数', '强制次数', 'held峰值'], [
        [label[m], f"{metrics[m]['mean_gateway_wait_ms']:.2f}", f"{metrics[m]['mean_submitted_to_read_ms']:.2f}",
         cases[m]['admission']['heavy_classified'], cases[m]['admission']['denied'], cases[m]['admission']['forced'], cases[m]['admission']['held_peak']] for m in modes]) + '\n'
    s += '重读标记数统计下发给实例的storage_hint；旧网关模式不下发此标记，0不表示没有重读请求。网关等待发生在转发前；转发到读取还包含路由、worker接收、实例排队、配额等待与调度，不能全部解释成单次RPC延迟。held峰值表示模型内占账数量。强制模式允许符合条件的实例暂时超过中央cap，增加并发也会增加带宽竞争。\n\n'
    s += '本轮已移除load_submitted、layer_load_submitted、load_completed、no_external_load四类中央执行反馈RPC；准入、回滚、强制通知及首token释放仍然在线。读取事实由worker本地日志结束后补入报告，不作为在线策略的信息输入。更早报告的通信、模型、T0、GC或预热条件可能不同；下面仅对比已逐项核实的100GB/s那一轮。\n\n'
    s += '本轮五组同源码；运行文件与上一轮100GB/s逐文件指纹一致，native扩展也一致。相较100GB/s只修改共享池和准入参考带宽，直接组合f=1及其他配置不变。T0按90GB/s重新计算，期限随既有规则变化；计算单请求时间本身不变。两轮各方案均仅运行一次，实际到达、路由同分选择及宿主开销仍有波动。\n\n'
    s += table(['方案','100完成ms','90完成ms','完成变化%','100平均ms','90平均ms','平均变化%'],[
        [label[m],*[f"{cases[m]['comparison100'][k]:.2f}" for k in ('completion_100_ms','completion_90_ms','completion_change_pct','mean_100_ms','mean_90_ms','mean_change_pct')]] for m in modes])+'\n'
    s += table(['方案','100原期限SLO%','90新期限SLO%','变化百分点','90沿用100期限SLO%'],[
        [label[m],*[f"{cases[m]['comparison100'][k]:.2f}" for k in ('slo_100_pct','slo_90_pct','slo_change_pp','slo_90_on_100_deadlines_pct')]] for m in modes])+'\n'
    s += '时间变化为正表示90GB/s更慢。准入参考带宽参与额度计算，因此配额也按原规则随带宽变化。主表SLO仍按各轮原有T0规则；最后一列只将本轮TTFT与同请求的100GB/s期限比较，不改变执行。两轮的汇总及逐请求CSV均保存，旧数值没有被重算或覆盖。[带宽对照数据](report_data/bandwidth_comparison.csv)。\n\n'
    faster=[label[m] for m in modes if cases[m]['comparison100']['completion_change_pct']<0]
    if faster:
        s += '本轮'+ '、'.join(faster)+'在90GB/s的完成时间更短。各轮仅一次；不能据此认定降低带宽改善性能，实际到达、路由与组批及宿主调度仍可能影响结果。\n\n'
    if all(abs(cases[m]['comparison100']['slo_90_on_100_deadlines_pct']-cases[m]['comparison100']['slo_90_pct'])<1e-10 for m in modes):
        s += '本轮所有方案沿用100GB/s期限后，达标请求数均未变化。因此，上表SLO差异不是这次期限重算造成的。\n\n'
    s += '## 7. 计算模型与结论限制\n\n'
    s += '- 按各请求本步真实形状分别使用v2单请求估时，再逐层相加；仅扣max(n-1,0)×3×7µs×78的重复kernel启动。固定TP/DP/EP，权重读取及专家通信逐请求累加。启动合并是仿真假设，未做目标设备定标。\n'
    s += '- 12个逻辑执行实例与公式中的参考DP8/EP8是不同层次；本轮没有真实多卡模型分片或同步专家通信。\n'
    s += '- 每组一次。路由同分选择、到达迟到、宿主调度和组批变化都可能改变结果，不提供稳定收益或统计显著结论。\n'
    s += '- 只做prefill并输出1 token。本地前缀缓存关闭，人为96%外部命中不是原trace实测命中率；不覆盖持续decode、真实存储容量淘汰、真实KV张量和跨机网络。\n'
    s += '- 暂停GC仅在受保护测量窗口内生效；本地记录、sleep、存储轮询和真实控制程序仍计入壁钟。\n\n'
    s += '## 8. 附表：实际投递与资源\n'
    s += image('07_arrival_lateness', '实际客户端发送时间减计划发送时间，保留全部迟到值。五组计划相同不等于实际到达完全相同；它与网关排队不是同一段时间。')
    s += table(['方案', '发送迟到均值ms', '发送迟到p95 ms', '发送迟到最大ms', '客户端完成ms'], [
        [label[m], *[f'{metrics[m][k]:.2f}' for k in ['lateness_mean_ms', 'lateness_p95_ms', 'lateness_max_ms', 'client_completion_ms']]] for m in modes]) + '\n'
    s += table(['方案', '整轮壁钟s（含启动/预热/验收）', '子进程RSS峰值GiB', '最低可用内存GiB', '观察到的swap峰值GiB'], [
        [label[m], *[f"{read(data/'raw'/m/'driver.json')[k]:.3f}" for k in ['wall_s', 'peak_children_rss_gib', 'min_available_gib', 'max_swap_gib']]] for m in modes]) + '\n'
    s += '## 9. 验证、数据与复现\n\n'
    s += '两实例6请求强制准入冒烟通过后，依次新跑五组。五组均完成360正式＋72预热，12/12实例参与。首token及finish=length逐请求检查；HTTP 200没有被单独当作成功证据。\n\n'
    failures=read(data/'matrix_receipt.json')['prior_failed_attempts']
    if failures:
        s += f'另保留{len(failures)}次失败整轮尝试，不进入性能统计：\n\n'
        for failure in failures:
            s += f"- {failure['physical_case']}："
            s += '；'.join(f"{r['phase']}首token完整{r['first_tokens']}/{r['total']}（HTTP成功{r['http_ok']}）" for r in failure['warmup_phases'])
            s += '。'
            if failure['errors']:s += failure['errors'][0]['line']+'。'
            s += '正式计时尚未开始。随后在新目录重启整组，配置、输入、源码、超时和协议均不变，没有重放单个状态不明RPC。\n'
        s += '\n失败说明原生通信仍有未定位的超时风险，成功重跑不代表该问题已修复。[全部尝试记录](report_data/attempts.json)保留状态与阶段信息。\n\n'
    s += table(['方案', '正式请求', '模型step', '层数/请求', '公式最大误差s', '字节积分误差B'], [
        [label[m], checks[m]['formal'], checks[m]['model_steps'], checks[m]['layers_per_request'], f"{checks[m]['formula_max_error_s']:.3g}", f"{checks[m]['bandwidth_error_bytes']:.6f}"] for m in modes]) + '\n'
    s += '已核对实际token、字节、逐层屏障、分块、EDF排序、配额与路由记账排空、GC恢复、资源保护、输入/运行源码/native指纹。额外独立验证逐请求主表、60份实例时间账本、CDF排序概率质量、10ms带宽积分、文档链接和ZIP。图表最终状态见验证清单。\n\n'
    s += '- [逐运行统计](report_data/run_summary.csv)、[逐请求统计](report_data/requests.csv)、[实例时间账本](report_data/device_utilization.csv)。\n'
    s += '- [带宽10ms桶](report_data/bandwidth_10ms.csv)、[原区间](report_data/storage_intervals.csv)、[CDF样本](report_data/cdf.csv)、[准入统计](report_data/admission_summary.csv)。\n'
    s += '- [输入核对](report_data/input_check.json)、[冻结来源和文件摘要](report_data/provenance.json)、[五组运行收据](report_data/matrix_receipt.json)、[独立验证清单](report_data/validation.json)。\n'
    s += '- [固定重绘脚本](reproduce/redraw_timeline.py)、[时间线替换入口](refresh_timeline.py)、[重绘校验](online_timeline/validation.json)、[分类明细](online_timeline/requests.csv)。\n'
    s += '- 运行编排及既有RPC诊断补丁在reproduce/；原始Linux数据目录见provenance.json。完整输入token、服务日志和fine_trace留原审计目录，ZIP包含图表重绘所需投影与脚本。\n'
    s += '- 在具有NumPy、Matplotlib、Pillow和中文字体的Python环境中运行build_report.py可重绘。逐张检查PNG后运行verify_report.py --finalize --visual-reviewed生成最终清单与分享ZIP。重跑服务还需固定的DV环境、输入和native扩展。\n\n'
    s += '## 10. 简要解读\n\n'
    s += f"本轮可先看{label[highest]}的SLO与{label[fastest]}的完成时间，再看A/B分组和时间线。比较新TFS与强制准入，观察缩短空闲是否同时增加带宽竞争或伤害短请求。计算压力应使用各组实际batch时间；不能将单请求参考压力、模型计算占比和真实GPU利用率混为一个指标。\n"
    return s
