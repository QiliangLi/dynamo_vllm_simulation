# Benchmark目录

本表从各文件夹的 manifest.json 生成。新增负载后运行 python benchmark/manage.py catalog 更新。

| Benchmark（点击查看说明） | 来源 / 特征 | 请求数 / 窗口 | 实例 / 带宽 | 新计算 token（比例） | 读取 GB | 类别数量 | 计算模型 / 反馈 | 支持状态 |
|---|---|---|---|---|---|---|---|---|
| [agentx_flex_358_12s_4_25GBps](agentx_flex_358_12s_4_25GBps/README.md) | agentx / flex | 358 / 12s | 4 / 25 GB/s | 2418880（18.473081%） | 699.609907 | A:120/B:150/C:88 | prefill_fit / first_token | [四实例入口](../docs/四实例25GB运行指南.md) |
| [agentx_narrow_358_12s_4_25GBps](agentx_narrow_358_12s_4_25GBps/README.md) | agentx / narrow | 358 / 12s | 4 / 25 GB/s | 2018496（15.415333%） | 725.849473 | A:120/B:150/C:88 | prefill_fit / first_token | [四实例入口](../docs/四实例25GB运行指南.md) |
| [agentx_random_360_12s_12_180GBps](agentx_random_360_12s_12_180GBps/README.md) | agentx / random | 360 / 12s | 12 / 180 GB/s | 667280（2.869297%） | 2480.769528 | A:90/B:270 | glm5_direct_sum_v1 / disabled | 可运行 |
| [mooncake_abc_360_4s_12_100GBps](mooncake_abc_360_4s_12_100GBps/README.md) | mooncake / abc | 360 / 4s | 12 / 100 GB/s | 204699（3.999781%） | 539.571462 | A:240/B:90/C:30 | glm5_actual_batch_work_v2 / disabled | 可运行 |
| [mooncake_abc_360_4s_12_90GBps](mooncake_abc_360_4s_12_90GBps/README.md) | mooncake / abc | 360 / 4s | 12 / 90 GB/s | 204699（3.999781%） | 539.571462 | A:240/B:90/C:30 | glm5_actual_batch_work_v2 / disabled | 可运行 |
| [mooncake_abcd_360_4s_12_100GBps](mooncake_abcd_360_4s_12_100GBps/README.md) | mooncake / abcd | 360 / 4s | 12 / 100 GB/s | 204699（3.999781%） | 539.571462 | A:220/B:90/C:30/D:20 | glm5_actual_batch_work_v2 / disabled | 可运行 |
| [mooncake_abcd_360_4s_12_90GBps](mooncake_abcd_360_4s_12_90GBps/README.md) | mooncake / abcd | 360 / 4s | 12 / 90 GB/s | 204699（3.999781%） | 539.571462 | A:220/B:90/C:30/D:20 | glm5_actual_batch_work_v2 / disabled | 可运行 |
| [mooncake_head875_360_12s_4_25GBps](mooncake_head875_360_12s_4_25GBps/README.md) | mooncake / head875 | 360 / 12s | 4 / 25 GB/s | 634043（12.499756%） | 290.874982 | A:240/B:90/C:30 | prefill_fit / first_token | [四实例入口](../docs/四实例25GB运行指南.md) |
| [mooncake_random_360_4s_12_90GBps](mooncake_random_360_4s_12_90GBps/README.md) | mooncake / random | 360 / 4s | 12 / 90 GB/s | 207547（4.055431%） | 539.258683 | A:90/B:270 | glm5_actual_batch_work_v2 / disabled | 可运行 |
| [mooncake_syntheticback50_1997_115s_4_25GBps](mooncake_syntheticback50_1997_115s_4_25GBps/README.md) | mooncake / syntheticback50 | 1997 / 115s | 4 / 25 GB/s | 4932435（13.812412%） | 2017.049182 | remote_read:1414/zero_read:583 | prefill_fit / first_token | 仅校验、导出 |
| [mooncake_syntheticp625_720_35s_4_25GBps](mooncake_syntheticp625_720_35s_4_25GBps/README.md) | mooncake / syntheticp625 | 720 / 35s | 4 / 25 GB/s | 1781603（14.103848%） | 711.093912 | remote_read:486/zero_read:234 | prefill_fit / first_token | 仅校验、导出 |
| [synthetic_abc_360_6s_12_90GBps](synthetic_abc_360_6s_12_90GBps/README.md) | synthetic / abc | 360 / 6s | 12 / 90 GB/s | 318600（6.208058%） | 528.631235 | A:120/B:120/C:120 | glm5_direct_sum_v1 / disabled | 可运行 |
| [synthetic_abchalfb_408_8s_4_25GBps](synthetic_abchalfb_408_8s_4_25GBps/README.md) | synthetic / abchalfb | 408 / 8s | 4 / 25 GB/s | 595968（15.993405%） | 205.151797 | A:120/B:144/C:144 | prefill_fit / first_token | [四实例入口](../docs/四实例25GB运行指南.md) |
| [synthetic_abclonghit_540_12s_4_25GBps](synthetic_abclonghit_540_12s_4_25GBps/README.md) | synthetic / abclonghit | 540 / 12s | 4 / 25 GB/s | 1121280（23.934426%） | 233.538847 | A:60/B:240/C:240 | prefill_fit / first_token | [四实例入口](../docs/四实例25GB运行指南.md) |

## 来源与构造说明

- **agentx_flex_358_12s_4_25GBps**：按类调整 H/M。A 目标 15–20 GB/s，B 为 3–8，C 为 0–3。实际范围分别为 15.04–19.79、3.02–7.98、0–2.96 GB/s。 来源：AgentX (request IDs agentx:L…)，原行号 72863—73222。输入 SHA256：9f77ba7980ed962f6706e87e183278ea6240efce19aa896cdad45b8edeb6e13b。
- **agentx_narrow_358_12s_4_25GBps**：A 理论需求 18.04–19.44 GB/s，B 为 2.69–3.70，C 为 0–3.40。 来源：AgentX (request IDs agentx:L…)，原行号 72863—73222。输入 SHA256：ace4a4a3d4923770f295a13ccb42058bfa17ffdb7f0a93f1d00051a6813b74d9。
- **agentx_random_360_12s_12_180GBps**：保留选定AgentX窗口的请求顺序，用原组完整历史推导理想远端前缀命中；不是冷启动或实测命中，39条超长上下文涉及公式外推。 来源：semianalysisai/cc-traces-weka-062126，原行号 2608—2967。输入 SHA256：eeafecc40e2eb2e5185875e872716056531daecb13bcb768fa9555efacf2e707。
- **mooncake_abc_360_4s_12_100GBps**：保留相同长度和到达节奏；全局约4%新算预算，人工构造A240/B90/C30。 本版本仅将共享池容量和准入参考带宽调整为100 GB/s；未重跑性能实验。 来源：data/status_aware_network/mooncake_trace/conversation_trace.jsonl，原行号 1—360。输入 SHA256：afb2b6c2f8015bcd224410b0e9e6d9d626115c2a85b8d89dd2bc7e43ee65c308。
- **mooncake_abc_360_4s_12_90GBps**：保留相同长度和到达节奏；全局约4%新算预算，人工构造A240/B90/C30。 来源：data/status_aware_network/mooncake_trace/conversation_trace.jsonl，原行号 1—360。输入 SHA256：afb2b6c2f8015bcd224410b0e9e6d9d626115c2a85b8d89dd2bc7e43ee65c308。
- **mooncake_abcd_360_4s_12_100GBps**：保留相同总读量与全局新算预算；从B转移40640个新token给最长20条D，构成A220/B90/C30/D20。 本版本仅将共享池容量和准入参考带宽调整为100 GB/s；未重跑性能实验。 来源：data/status_aware_network/mooncake_trace/conversation_trace.jsonl，原行号 1—360。输入 SHA256：ebbbda06f7f99dd836867555d9fbef4af2c848d79dc699b74a9a54ccbd0acdf7。
- **mooncake_abcd_360_4s_12_90GBps**：保留相同总读量与全局新算预算；从B转移40640个新token给最长20条D，构成A220/B90/C30/D20。 来源：data/status_aware_network/mooncake_trace/conversation_trace.jsonl，原行号 1—360。输入 SHA256：ebbbda06f7f99dd836867555d9fbef4af2c848d79dc699b74a9a54ccbd0acdf7。
- **mooncake_head875_360_12s_4_25GBps**：Mooncake开头360条，A240/B90/C30，总token命中约87.5%；B/C前缀保持，A按长度分配剩余命中预算。沿用三条超长输入截断。 来源：mooncake_head875_v80p7sor/trace.jsonl，原行号 1—360。输入 SHA256：8e2b54aef1a915a106687c1710ba4cef2be7b8dc07aa8111589b8b7308792796。
- **mooncake_random_360_4s_12_90GBps**：保留Mooncake前360条原长度与顺序，逐请求人为96%远端命中后block16取整；不是未经处理的随机命中。 来源：data/status_aware_network/mooncake_trace/conversation_trace.jsonl，原行号 1—360。输入 SHA256：57b23ce7ac589220f23a5ce7b1ba768f786fa9506c41d58ddbb3ffa62f315f9b。
- **mooncake_syntheticback50_1997_115s_4_25GBps**：Synthetic 后半段，1,997 条，暖缓存命中 86.19%，115 秒，六盘共享 25 GB/s。 来源：Mooncake synthetic_trace.jsonl，原行号 1997—3993。输入 SHA256：35cbdd47d42f7131d1c1ed1bcd4c5434ec7a03aab5ea70222d307eca96080789。
- **mooncake_syntheticp625_720_35s_4_25GBps**：Synthetic 第62.5%窗口，720条，暖缓存命中85.90%，35秒，6盘共享25 GB/s。 来源：Mooncake synthetic_trace.jsonl，原行号 2496—3215。输入 SHA256：d79b922424f300c66ade34b1cb095a9cf02324443282999480645aaabeb2d68d。
- **synthetic_abc_360_6s_12_90GBps**：原seed416人工ABC到达顺序，固定A30720/259、B9392/2048、C0/348；最新GLM78层读算重定标，6秒从时间0起算，首请求间隔保留。 来源：E26f synthetic C_A seed416, frozen block16 ABC request order，原行号 1—360。输入 SHA256：3148c9e7cf8952f9c016ba9ac7e1db8993780d96bb2146f99d7abcfb01ec40b6。
- **synthetic_abchalfb_408_8s_4_25GBps**：A120: H20480/U2048；B144: H4608或4736/U2048；C144: H0/U384。8秒到达，逐层提交。 来源：abc408_repeat_dlwq4a_6/half_b/r1/trace.jsonl，原行号 1—408。输入 SHA256：6ddd099448964e1208de96e7a1a3cee422a6e570fc2ab2cdf0e0f692092690c5。
- **synthetic_abclonghit_540_12s_4_25GBps**：A60: H51200/U768；B240: H2048/U4096；C240: H0/U384。12秒到达。 来源：abc540_12s_1p8qr14z/trace.jsonl，原行号 1—540。输入 SHA256：678ec1850c2a6cb1076ff439cf877b50e02bdf4eadb394cd14a6aa3e2e850eed。

## 附录：字段与类别

- server_stack_v1：本次四实例MiniMax固定输入，保留原始token和计划到达，不重采样。窗口表示0到标称截止时刻，末次请求可早于截止。目录标有四实例入口的用例可用专用运行器。仅校验、导出条目见各自说明。
- 名称顺序：数据来源_数据特征_请求总数_发送窗口_计算实例数_存储带宽。时间用 s；GBps 表示十进制 GB/s，1 GB = 10^9 B。带宽指单共享池，不是每实例独享带宽。
- random：未按 ABC/ABCD 人为分配请求类别。不表示随机打乱、无采样或命中未处理。两份 random 的命中规则不同，见来源说明及各自 manifest。
- mooncake 来源的 abc / abcd：总 input 的约 4% 为全局新算预算。A 重读轻计算（新算 1—16 token）；B 轻读重计算（承接剩余额度）；C 零读（短请求全部重算）；D 重读重计算（最长 20 条各新算 2033—2046 token）。按长度选类别后恢复原 trace 顺序，不按类别重排。
- synthetic 来源的 abc：固定三种请求形状，A 重读轻计算、B 较轻读且计算更重、C 零读且计算时间接近 A。h/u 取自 manifest 的 class_shapes，不使用 Mooncake 全局 4% 规则；保留 seed416 原到达顺序。
- 表中 A/B/C/D 是人工或离线展示标签，不是运行期 TFS P75 轻重分类。random 的 A/B 不按 ABC 构造解释。
- 新计算比例 = sum(u)/sum(input)。逐请求约 4% 与整批全局约 4% 不等价；block16 影响取整。h 为有效远端命中 token，u 为新计算 token，input=h+u。
- 默认窗口是首个计划到达到最后一个计划到达的间隔；manifest 有 arrival_mapping 时按其时间原点到末次计划到达定义，保留首请求之前的间隔。实际发送迟到和整轮完成时间另行统计。实例数为逻辑实例数，不等于物理 GPU/NPU 数量。
- config.json 是本负载运行配置，config.historical.json 只用于追溯。模型版本、反馈、block、SLO 以配置及合同为准，不从名称猜测。T0/期限按当前配置重算，不能复用另一模型或带宽下的旧期限。
- 可运行：当前交付运行器支持。仅校验、导出：不得套用其他负载默认条件。AgentX 有 39 条超过 131072 token，最长 300480，涉及公式外推。所有计算时间均为解析仿真，不是实机测量。
