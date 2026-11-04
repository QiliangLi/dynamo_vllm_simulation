# 独立多盘六负载、五策略总览

本任务完成30次新的正式运行，共10800条正式请求、2160条预热请求。六组冒烟先通过。每种组合只有一次有效运行，没有跨重复置信区间。所有TFS均用EDF；只有直接准入FCFS使用FCFS。

|负载|独立磁盘|SLO最高方案|SLO%|完成最快方案|完成ms|完整报告|
|---|---|---|---:|---|---:|---|
|agentx_random_360_12s_12_180GBps|6×30 GB/s|新 TFS EDF α=0.1|5.83|旧 TFS EDF|39945.5|[查看](../../agentx_random_360_12s_12_180GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1/report.md)|
|mooncake_abc_360_4s_12_100GBps|4×25 GB/s|旧 TFS EDF|6.67|新 TFS EDF α=0.1|12658.4|[查看](../../mooncake_abc_360_4s_12_100GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1/report.md)|
|mooncake_abcd_360_4s_12_100GBps|4×25 GB/s|新 TFS EDF α=0.1|4.44|新 TFS EDF α=1|12099.2|[查看](../../mooncake_abcd_360_4s_12_100GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1/report.md)|
|mooncake_abc_360_4s_12_90GBps|3×30 GB/s|旧 TFS EDF|11.11|新 TFS EDF α=0.1|11179.9|[查看](../../mooncake_abc_360_4s_12_90GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1/report.md)|
|mooncake_abcd_360_4s_12_90GBps|3×30 GB/s|旧 TFS EDF|7.50|新 TFS EDF α=1|10653.1|[查看](../../mooncake_abcd_360_4s_12_90GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1/report.md)|
|synthetic_abc_360_6s_12_90GBps|3×30 GB/s|旧 TFS EDF|48.61|旧 TFS EDF|10709.3|[查看](../../synthetic_abc_360_6s_12_90GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1/report.md)|

## 五方案完整结果

|负载|方案|SLO%|相对直接EDF pp|完成ms|平均TTFT ms|名义计算%|
|---|---|---|---|---|---|---|
|agentx_random_360_12s_12_180GBps|直接准入 FCFS|3.06|-1.94|45209.2|17908.3|30.29|
|agentx_random_360_12s_12_180GBps|直接准入 EDF|5.00|+0.00|47939.5|16134.5|28.57|
|agentx_random_360_12s_12_180GBps|旧 TFS EDF|5.56|+0.56|39945.5|15302.1|34.44|
|agentx_random_360_12s_12_180GBps|新 TFS EDF α=0.1|5.83|+0.83|46409.8|14655.7|29.50|
|agentx_random_360_12s_12_180GBps|新 TFS EDF α=1|3.89|-1.11|44379.6|14715.3|30.87|
|mooncake_abc_360_4s_12_100GBps|直接准入 FCFS|3.61|+0.28|14549.4|5661.7|21.49|
|mooncake_abc_360_4s_12_100GBps|直接准入 EDF|3.33|+0.00|15883.2|5612.9|19.92|
|mooncake_abc_360_4s_12_100GBps|旧 TFS EDF|6.67|+3.33|12762.6|3674.4|27.60|
|mooncake_abc_360_4s_12_100GBps|新 TFS EDF α=0.1|4.17|+0.83|12658.4|3834.4|27.77|
|mooncake_abc_360_4s_12_100GBps|新 TFS EDF α=1|3.61|+0.28|13782.9|5000.9|25.38|
|mooncake_abcd_360_4s_12_100GBps|直接准入 FCFS|2.78|+0.28|13815.7|5338.4|23.56|
|mooncake_abcd_360_4s_12_100GBps|直接准入 EDF|2.50|+0.00|15096.0|5691.0|21.52|
|mooncake_abcd_360_4s_12_100GBps|旧 TFS EDF|3.33|+0.83|12965.4|4254.1|27.89|
|mooncake_abcd_360_4s_12_100GBps|新 TFS EDF α=0.1|4.44|+1.94|12112.1|4230.0|29.42|
|mooncake_abcd_360_4s_12_100GBps|新 TFS EDF α=1|2.78|+0.28|12099.2|4338.7|29.40|
|mooncake_abc_360_4s_12_90GBps|直接准入 FCFS|4.72|+1.39|11958.8|4258.8|26.12|
|mooncake_abc_360_4s_12_90GBps|直接准入 EDF|3.33|+0.00|12336.7|4174.0|25.62|
|mooncake_abc_360_4s_12_90GBps|旧 TFS EDF|11.11|+7.78|11562.0|2627.6|31.11|
|mooncake_abc_360_4s_12_90GBps|新 TFS EDF α=0.1|7.22|+3.89|11179.9|3021.0|31.13|
|mooncake_abc_360_4s_12_90GBps|新 TFS EDF α=1|2.22|-1.11|11582.9|3464.0|30.09|
|mooncake_abcd_360_4s_12_90GBps|直接准入 FCFS|4.44|+0.28|10894.8|3958.2|29.96|
|mooncake_abcd_360_4s_12_90GBps|直接准入 EDF|4.17|+0.00|12987.4|3921.0|25.33|
|mooncake_abcd_360_4s_12_90GBps|旧 TFS EDF|7.50|+3.33|10918.7|2985.5|33.75|
|mooncake_abcd_360_4s_12_90GBps|新 TFS EDF α=0.1|3.89|-0.28|10952.1|3423.6|32.57|
|mooncake_abcd_360_4s_12_90GBps|新 TFS EDF α=1|5.28|+1.11|10653.1|3201.4|33.79|
|synthetic_abc_360_6s_12_90GBps|直接准入 FCFS|2.50|-0.28|11187.8|3518.5|51.87|
|synthetic_abc_360_6s_12_90GBps|直接准入 EDF|2.78|+0.00|11705.6|3413.0|49.57|
|synthetic_abc_360_6s_12_90GBps|旧 TFS EDF|48.61|+45.83|10709.3|1968.9|54.41|
|synthetic_abc_360_6s_12_90GBps|新 TFS EDF α=0.1|33.33|+30.56|10714.5|2231.7|54.31|
|synthetic_abc_360_6s_12_90GBps|新 TFS EDF α=1|20.28|+17.50|11598.8|2783.4|50.15|

## 如何解释

每份报告含8张图：SLO、在线轻重时间线、名义计算占比、总带宽、总TTFT CDF、分组CDF、到达迟到、逐盘带宽。图8用于看磁盘热点；图4的总带宽不能代替单盘压力。

本轮单次记录中，旧TFS的SLO在两份Mooncake ABC、Mooncake ABCD 90 GB/s及人工ABC中最高；新TFS α=0.1在AgentX和Mooncake ABCD 100 GB/s中最高。AgentX的两者差距只有1条达标请求，不能据此认定稳定优势。六组工况的最佳SLO仍均低于50%。

100 GB/s与90 GB/s分别使用4盘和3盘，也分别运行一次，包含独立的路由和宿主时序；出现完成时间倒挂时，不能仅归因于总带宽。当前矩阵没有同拓扑、多次重复的带宽单变量定标。

本次只把已有HRW多盘存储模型接入冻结实验入口，没有新增逐盘准入或路由。TFS仍按总参考带宽执行全局配额。新旧TFS同时区别准入位置和轻重分类时机；α=1不能作为“等价旧TFS”的依据。

AgentX和人工ABC保留direct_sum；四份Mooncake保留actual_batch_work_v2。不同负载不合并CDF、不汇总总体胜率。AgentX有39条上下文公式外推请求。红色不是纯存储等待，灰色不等于实例没有排队请求，名义计算占比不是硬件利用率。

## 验证与保留的失败

336项完整回归、6组冒烟及30组正式验收通过。r1曾因1秒指标采样遗漏短冒烟的正预留而失败；已保留原因与原生raw_prefill_blocks证据，修正验收后从r2开始全部正式运行。每次有效运行检查输入、有效配置、模型公式、层屏障、真实队列、原生路由、额度归零、RPC排空与进程退出。带宽重建另核对每次读取完成和逐盘字节积分。所有新图逐张检查，公开压缩归档再做SHA、表格、图像和链接校验。

本地保留大型原始事件；GitHub只发布报告、标准派生数据、小型验收凭证和固定重绘工具。每份manifest的source_execution是实际性能运行；simulations_run=0仅指后续归档没有再跑实验。
