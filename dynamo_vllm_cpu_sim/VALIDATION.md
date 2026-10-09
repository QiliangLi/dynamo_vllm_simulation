# 验证记录

日期：2026-09-24。以下为 CPU 仿真虚拟时间，**不是 NPU 实测性能**。

环境：`Linux-6.18.44-x86_64-with-glibc2.39`；Python 3.12.14；torch 2.11.0+cpu；ai-dynamo-runtime 1.5.0；vLLM 0.20.2+empty。

每项 12 个请求，每请求 8 个输出 token，两个逻辑 worker。具体配置和请求级记录保存在各 results 子目录。

| 模式 | 完成数 | 平均 TTFT (ms) | p95 TTFT (ms) | makespan (ms) | 读取量 (MiB) |
|---|---:|---:|---:|---:|---:|
| vLLM 原始调度器 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| Ascend 默认委托路径 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| Ascend balance + 显式补丁 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| vLLM 原生 priority | 12 | 5.234561 | 13.674912 | 15.164912 | 48 |
| 慢共享链路 | 12 | 252.649073 | 672.180640 | 673.670640 | 64 |
| 默认路径复跑 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| 新建虚拟环境 bootstrap | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |

通过的集成断言：所有请求完成且输出数正确；真实远端 KV 等待和接收事件存在；首 token 不早于依赖 KV 接收；每 worker 未完成请求为 0、可回收块为 127/128（一个 null sentinel）；Dynamo active_requests、potential_prefill_tokens、potential_decode_blocks 及 pending_count 均为 0。

存储单元测试 3/3 通过：中途新增并发读会改变已有读的完成时间；同路径按 FIFO 服务；启动延迟与共享链路上限生效。

慢存储将 shared_link_Bps 从 10,000,000,000 调到 100,000,000。共享链路变慢还会改变原生负载与后续路由/本地缓存复用，所以读量由 48 MiB 变成 64 MiB；这不是固定读量的纯带宽微基准。

priority 在此小例子中的均值差异只用于说明真实策略路径可执行，不能证明其优于 FCFS。p95 对 12 个请求也不具备业务统计代表性。

默认复跑的汇总时间相同；原生 picker 的平局选择可能让请求的 worker ID 对调，因此没有宣称逐事件 bitwise deterministic。

Ascend balance 使用 scripts/apply_ascend_patch.py 产生的显式副本，原始 upstream 文件未修改；未做 NPU 验证。其他模式均使用原始目标调度源码。

验证范围不含完整 EngineCore、全量 Ascend 插件、HTTP 服务、cache-aware Dynamo、PD、多卡通信、逐层 overlap、写回、取消、失败和抢占压力。详细边界见设计文档。

追加验收：在不含 upstream、.venv 或 patched 目录的新工程副本中运行同一 bootstrap.sh，成功按三个固定 SHA 下载源码、安装依赖、通过 doctor 并跑通全部请求和集成断言。报告见 results/clean_bootstrap，下载记录见 results/source-downloads.json。网络使用当前执行环境提供的 HTTPS 代理；工程脚本本身不设置代理。

## 追加：macOS Apple Silicon 原生运行（2026-09-24）

环境：macOS 26.6、Apple Silicon（arm64）、CPython 3.12.10（uv 提供）、torch 2.11.0（无 `+cpu` 后缀的 mac 构建）、vllm 0.20.2+empty、ai-dynamo-runtime 1.5.0（从同一固定 SHA 源码 maturin 编译，`--features select-service`）。适配细节见 README 的 macOS 小节；其中 doctor 的 torch 精确版本检查按预期失败（mac 无 `+cpu` 后缀），未作为通过条件。

五模式全部完成，`check_results.py` 生命周期/远端等待/块回收/Dynamo 记账断言全部通过。虚拟时间汇总与上表 Linux 记录逐位一致（worker 标签可能因原生 picker 平局随机对调，汇总不变）：

| 模式 | 完成数 | 平均 TTFT (ms) | makespan (ms) | 读取量 (MiB) | 报告 |
|---|---:|---:|---:|---:|---|
| 默认（Ascend 委托） | 12 | 5.364061 | 15.164912 | 48 | results/mac_default |
| vLLM 原始调度器 | 12 | 5.364061 | 15.164912 | 48 | results/mac_upstream |
| vLLM 原生 priority | 12 | 5.234561 | 15.164912 | 48 | results/mac_priority |
| 慢共享链路 | 12 | 252.649073 | 673.670640 | 64 | results/mac_slow |
| Ascend balance + 显式补丁 | 12 | 5.364061 | 15.164912 | 48 | results/mac_ascend_balance |

存储单元测试 3/3 通过。macOS 路径涉及两处受控偏差并已在 README 记录：torch 版本串差异、vLLM `setup.py` 允许 darwin 上显式 `empty` 的一行修改。数值为合成模型验证，仍不代表 NPU 性能。

## 追加：A/B 双类带宽争抢实验（2026-10-08）

设计见 `docs/AB双类带宽争抢实验设计-20261008.md`，结果文档（含四图）见 `AB双类带宽争抢实验结果-20261008.md（仓库根）`。环境：macOS 26.6 Apple Silicon、同上虚拟环境；`configs/ab.json`（32 worker、共享链路 120 GB/s 唯一瓶颈、`max_num_seqs=1` 映射 batch=1、`storage_interval_log` 开启、`VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` 放行 139264 上下文）。负载 192 请求（64A+128B、每轮 32A+64B 同刻到达 ×2 轮、轮距 1.0s），两臂同 config、同到达时刻，仅到达顺序不同；trace 由 `scripts/gen_ab_trace.py` 生成（token 内容每请求唯一，杜绝本地前缀缓存命中）。虚拟 token 标定：A 重算 862/字面 256、B 字面 4096，每类读字节与计算时间命中 E26 大纲给定值（误差 ≤0.05%）。下表为补记存储区间日志后的重跑数值（调度路径无变化；见文末复现性注记）：

| 臂 | 完成数 | makespan (s) | 读取量 (GB) | A 类 TTFT mean/p95 (ms) | A 类 SLO | B 类 TTFT mean/p95 (ms) | B 类 SLO | 并发 A 峰值/p90 | 报告 |
|---|---:|---:|---:|---|---:|---|---:|---|---|
| 块状（32A 在前） | 192 | 2.2080 | 129.3547 | 363.6 / 587.5 | 28.1% | 708.4 / 1048.1 | 83.6% | 32 / 31 | results/ab_block_b1.0 |
| 交错（(A,B,B)×32） | 192 | 3.1118 | 129.3547 | 632.2 / 1355.4 | 18.8% | 856.0 / 1577.0 | 65.6% | 36 / 34 | results/ab_interleave_b1.0 |

按类 SLO = α×单条理想 TTFT（α=4：A 239.4ms、B 925.1ms）。两臂 compute 合计逐位一致（32.671s）、读取量逐位一致（129.3547 GB = trace 声明），CRN 成立；块状臂 stall 19.7s/idle 18.3s，交错臂 stall 17.7s/idle 49.2s；两臂链路饱和（≥90%×120GB/s）时长均 1.076s ≈ 理论排空下限 2×0.539s。冒烟（8A+16B，results/ab_smoke）先通过：24 请求、16.169 GB、系统级并发远端等待峰值 24。

集成断言（check_results.py）两臂全 PASS（生命周期/远端等待先于首 token/块回收 65535/65536/Dynamo 记账归零）。单元测试 11/11（存储 4 项含 link_flux 区间守恒 + trace 生成器 7 项：顺序/计数、过 sim.run.validate、每类读字节命中 1.4052/0.30798 GB（0.01%）、确定性、前缀互异）。

**图与守恒**：`scripts/ab_fig.py` 产出分类甘特与带宽时序四图（`docs/figures/ab_{gantt,bw}_{block,interleave}.png`），`--audit` 全部 PASS：甘特三分时间与 report 每 worker compute/stall/idle 一致（最大相对误差 2.4e-16）、带宽 ∫actual·dt = bytes_read（相对误差 1.1e-11）、实际峰值恰为链路上限 120 GB/s、需求峰值=盘聚合 240 GB/s。甘特重建以步完成事件反推计算区间（[t_end−duration, t_end]），前提为本 config prefill/decode 系数相等。

**复现性注记**：同 trace 同 config 复跑一轮对照显示，Dynamo 原生 picker 平局随机使 ~185/192 请求 worker 落点变化——均值类指标漂 0.3~2.1%、最值类（makespan/p95）漂 1.2~10.9%，bytes_read 与 compute 合计逐位不变。12 请求 demo 中该随机表现为"worker 标签对调、汇总不变"；192 请求非对称负载下放大到汇总层。判读规则：均值类 ±2% 内视为同分布；makespan/p95 单次运行不足以下细粒度结论（正式对照需确定性 picker 或多种子，见检视报告 §2.3.4/§8）。

结果解读（虚拟时间，非 NPU 实测）：E26d 的"块状→洪水、交错→天然错峰"不迁移——异步 KV 加载下两臂并发 A 峰值 32/36（4.1 条即打满链路，超订 ~7.8×），A 类 TTFT 均为读分摊主导（~360ms ≈ 1.405GB ÷ 120/32 GB/s）；到达序的实际作用点是每 worker 计算队列次序（块状臂 A 全部首位；交错臂 A 随队列深度 TTFT 345→1533ms 单调恶化）。含义：真实 vLLM 语义下读不受准入控制，错峰需 admission 类策略（对应检视报告 §4.1 P/B 层缺口）。

## 追加：存储 Path 语义变更与 max_min_fair 策略（2026-10-10）

依据 [docs/存储带宽分配策略设计-maxmin-20261009.md](../docs/存储带宽分配策略设计-maxmin-20261009.md) 实现（设计六项决策全部落地）：块哈希只落 ASU、对该盘全部 Path 可见，IO 请求发出时按确定性最少占用指派 Path——旧"块哈希 (盘, Path) 定位"废除，**两策略一致**。新增 `storage.bandwidth_policy = max_min_fair`（CLI `--bandwidth-policy`）：每个 IO 请求独占一条 Path、数量不限、无排队；每请求自携带需求 = kv_bytes_per_token / prefill_token_s（聚合模型下即一层传输量/一层计算时间，demo 配置推导值 32.768 GB/s/请求；trace 行 `demand_Bps` 可覆盖）；每盘带需求上限的水土填充迭代到不动点（供过于求：需求 + 剩余均分）；共享链路仍等比例压缩。`active_split` 保留"均分 × path_Bps 上限 × 链路"速率规则与每 Path FIFO。

**上文 2026-09-24 五模式表与 2026-10-08 A/B 表的数值均对应旧哈希定位语义，仅作历史记录**（A/B 两臂未在新语义下重跑，其机制性结论不受影响）。以下为新基线（环境：macOS 26.6 Apple Silicon、同虚拟环境；同 trace 同 config）：

| 模式 | 完成数 | 平均 TTFT (ms) | p95 TTFT (ms) | makespan (ms) | 读取量 (MiB) | 报告 |
|---|---:|---:|---:|---:|---:|---|
| Ascend 默认委托路径 | 12 | 5.326135 | 9.644353 | 11.234353 | 64 | results/ascend_default |
| vLLM 原始调度器 | 12 | 5.326135 | 9.644353 | 11.234353 | 64 | results/upstream |
| vLLM 原生 priority | 12 | 5.110165 | 10.455686 | 12.045686 | 64 | results/priority |
| 慢共享链路 | 12 | 447.982371 | 673.797307 | 675.387307 | 64 | results/slow |
| Ascend balance + 显式补丁 | 12 | 5.326135 | 9.644353 | 11.234353 | 64 | results/ascend_balance |
| **max_min_fair（新策略）** | 12 | 5.074758 | 9.400330 | 10.990330 | 64 | results/max_min_fair |
| 慢链路 × max_min_fair | 12 | 447.982371 | 673.797307 | 675.387307 | 64 | results/max_min_fair_slow |

`check_results.py` 七目录全 PASS（生命周期/远端等待先于首 token/块回收 127/128/Dynamo 记账归零）。单元测试 26/26：waterfill 向量断言（[10,28,100,100]→[10,28,31,31] 不动点、供过于求剩余均分、均匀需求退化为均分、单请求拿整盘、d>盘容量不预截断、Σd=C 边界）；Path 指派（最少占用铺开、超 paths_per_disk 退化排队、确定性）；max_min_fair（无排队语义、时间线解析对拍、link_flux 区间守恒 ∫actual=bytes）；active_split 速率规则回归。同 session 复跑两种策略各两次汇总逐位一致（原生平局随机在 12 请求 demo 表现为 worker 标签对调、汇总不变，与历史行为一致）。

判读（虚拟时间，非 NPU 实测）：demo 配置下 max_min_fair 比 active_split 快 2.2%（makespan 10.990 vs 11.234 ms）——差异来自取消 path_Bps=1 GB/s 单路径封顶（均匀推导需求 32.768 GB/s，两请求同盘即供不应求、各得 20 GB/s，链路压缩 0.25 后 5 GB/s，对 active_split 的 1 GB/s）。slow 配置（链路 0.1 GB/s 为唯一瓶颈）两策略汇总**逐位一致**：均匀需求下两策略同为均分、链路压缩后同为 0.1 GB/s 总量——印证设计文档 §3.5 的预期：链路主导时盘内分配策略对端到端不可见，B 层策略差异需在盘带宽为瓶颈的工况下观测。
