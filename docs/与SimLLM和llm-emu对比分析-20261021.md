# 与 SimLLM、llm-emu 的对比分析（2026-10-21）

> 本文回答："本仓库（dynamo_vllm_simulation）和 GitHub 上的 SimLLM、llm-emu 有什么区别？"
> 三个项目都属于"真实 LLM 服务框架控制面 + 仿真执行"这一同一家族，但研究问题、真实/仿真边界、时间模型、保真度目标几乎不重叠，属于互补而非竞品。
> 事实来源：本仓库 AGENTS.md / 包内 README / 各设计文档；SimLLM 官方 README（2026-10-21 抓取，仓库最后更新 2026-10-19）与第三方中文评述（知乎，2026-08-16）；llm-emu 仓库 README 与 OpenReview 论文页（MLArchSys@ISCA 2026 poster）；vLLM RFC #61305。

## 0. 结论先行（TL;DR）

| | 本仓库 | SimLLM | llm-emu |
|---|---|---|---|
| 一句话定位 | 真实 Dynamo 路由 + 真实 vLLM/vLLM-Ascend 调度代码 + 仿真执行与远端 KV 存储的**调度策略实验台** | **网络保真**的 LLM 部署仿真器：真实 vLLM/SGLang 调度面 + 包级 RNIC/Clos 网络仿真，预测采购/部署前的 TTFT/TPOT/良好吞吐 | **profile 驱动**的单实例 vLLM 在线仿真：CPU 主机上跑真实 `vllm serve`，仅 GPU 前向延迟由实测 profile 包预测 |
| 研究对象 | 路由/准入/调度策略（S/P/B 三层、带宽协同、MPC 在线控制） | 多节点集群的网络效应（incast、拥塞控制振荡、MoE all-to-all） | 特定 GPU+模型组合上的服务指标复现（无需 GPU） |
| 真实部分（独特） | **真实分布式路由层**：Dynamo frontend(HTTP OpenAI)+router+N×真实 vLLM worker 进程（真实 HTTP/TCP/ZMQ）；DES 形态直接调用真实 Rust SelectionService 与 vllm-ascend BalanceScheduler | 真实 vLLM v0.26.0 / SGLang 调度器、组批、KV 前缀记账（零 fork，经 executor 插槽接入） | 真实 vLLM v0.18.1 全服务路径：scheduler、KV 管理、准入、tokenizer、HTTP |
| 仿真的物理层 | NPU 计算（线性 token-时间系数）+ **远端 KV 存储与带宽**（ASU/Path、max-min fair、共享链路）——带宽争抢本身是研究客体 | GPU 执行（roofline/profile 表/trace）+ 设备模型（SM/HBM/NVLink/PCIe）+ **分组级包级网络**（htsim RNIC、DCQCN、Clos） | 仅每步 GPU 前向延迟（profile 包 + 自适应 Shepard 插值 oracle） |
| 时间模型 | **双形态**：DES 虚拟时钟（µs 级、确定性、可深拷贝重放）+ 进程栈壁钟（协议正确性） | 虚拟时钟；开环（GOAL trace 一次回放）与闭环（仿真完成时间推进调度器时钟） | 壁钟：realtime 模式按预测延迟 `sleep`（对外是真 HTTP 服务） |
| 绝对时间保真 | 刻意不做（系数为工况绑定量，明确声明非任何 NPU 实测） | 分层推进：机制层强，物理时间标定进行中（NIC 对标 UCCL/DCQCN/HPCC，GPU 对标 A100/GH200，已知缺口公示） | 最高：六格复现实验误差多在 ±5% 内（最差 ~±10%） |
| 规模 | 单机 CPU；DES 32 逻辑 worker / 栈 2+ 真实 worker 进程 | 参考拓扑 8 节点 × 8 B100（64 GPU、每 GPU 400G NIC、两层 Clos） | 单 GPU 实例（uniproc executor） |

三者的共同信念（也是 vLLM RFC #61305 正在标准化的方向）：**调度与状态机的事实必须由框架真实代码持有，只有"物理执行耗时"可被替换**。区别在于替换的是哪一段物理（网络 / GPU 前向 / 存储 IO），以及保留了多厚的真实分布式控制面（本仓库独有真实路由层）。

## 1. 对比对象与溯源

### 1.1 基本信息

| | 本仓库（dynamo_vllm_simulation） | SimLLM | llm-emu |
|---|---|---|---|
| 仓库 | 本工程（私有开发中） | [openfabric-systems/simllm](https://github.com/openfabric-systems/simllm) | [AKafakA/llm-emu](https://github.com/AKafakA/llm-emu) |
| 定位描述 | 真实 Dynamo 路由 + 真实 vLLM/Ascend 调度代码 + 仿真执行的 CPU 调度实验台 | "Network-faithful simulation of LLM serving and training deployments" | vLLM 壁钟仿真插件（profile-driven online emulation） |
| 作者/背景 | 本项目 | Yifeng Wang / OpenFabric Systems；ETH Zurich、CSCS、SPCL、Stanford、NERSC 支持 | Wei Da、Evangelia Kalyvianaki；MLArchSys@ISCA 2026 poster |
| 首次发布 | — | 2026-08-03（51 星，2026-10-21 查询） | 2026 年（16 星、9 commits） |
| 语言/构建 | Python（上游含 Rust 扩展编译） | Python + C++（CMake ≥3.16、C++17、~250MB 子模块） | Python（vLLM 0.18.1 + ~170 行补丁 + ~1.7K LoC 插件） |
| 许可证 | 本项目 | Apache-2.0（后端 htsim BSD-2、ATLAHS MIT） | Apache-2.0 |
| 论文 | 设计文档系列（本仓库 docs/） | README + docs/（预注册研究），暂未见正式论文 | [LLM-Emu: Profile-Driven Native Online Emulation of LLM Serving](https://openreview.net/forum?id=9jrjM8ZQyn) |

**同名消歧**：GitHub 上另有多个 "SimLLM"：`CGCL-codes/SimLLM`（边缘推理任务相似度优化，Euro-Par 2026，非仿真器）、`quocnsh/SimLLM`、`SimLLMCultureDist`（LLM 文化应答分布）等。本文对比的是 openfabric-systems/simllm，即知乎评述《欢迎 SimLLM！大模型部署仿真终于等来真正的端到端闭环》（2026-08）所介绍、并被 vLLM RFC #61305 引用的那个"网络保真部署仿真器"。

### 1.2 各自要回答的核心问题

- **本仓库**：在共享远端 KV 存储 + 多 worker 的架构下，路由/准入/调度策略（错峰、带宽分配、KV 感知路由、MPC 在线滚动优化）如何影响系统行为？策略改动落在真实 Dynamo/vLLM/vllm-ascend 代码上是否可行、语义是否正确？——**策略与机制研究，产出是策略与挂载点**。
- **SimLLM**：买/租硬件之前，这个部署（拓扑、TP 切分、负载）的 TTFT/TPOT/良好吞吐/SLO 达成率是多少？64+ 节点下 incast、队列堆积、拥塞控制振荡、MoE all-to-all 如何重塑延迟？——**部署前预测与 serving-网络协同设计，产出是指标与归因**。
- **llm-emu**：没有 GPU 的机器上，能否把某 GPU+模型+量化组合上的真实 vLLM 服务行为（TTFT/TPOT/ITL/E2E/TPS）以个位数百分比误差复现出来？——**单实例指标复现，产出是被仿真出来的真实服务进程**。

## 2. 架构画像

### 2.1 本仓库：双形态——单进程 DES 与多进程真实栈

**形态 A：单进程事件驱动仿真（DES，虚拟时钟）**。在同一 Python 进程内直接构造真实对象：真实 vLLM 0.20.2（`VLLM_TARGET_DEVICE=empty` 安装）的 v1 Scheduler、KVCacheManager、Request、KVCacheConfig（chunked prefill、前缀缓存、抢占语义皆为原码）；真实 Dynamo（ai-dynamo-runtime 1.5.0，`select-service` feature 编译）的 Rust SelectionService 做路由与 reserve/prefill-complete/free 记账；vllm-ascend 0.20.2rc1 的 BalanceScheduler 按文件加载、可开补丁副本。仿真侧只有两件事：NPU 计算（每类线性 token-时间系数）与远端共享 KV 存储（块哈希落唯一 ASU、Path 为访问通道、`active_split`/`max_min_fair` 双带宽策略、动态共享链路）。全局虚拟时间事件推进，µs 级时序确定性；仿真世界（真实 Scheduler 对象 + Storage）可整体深拷贝且重放轨迹逐位一致（`verify_rollout_copy.py`，~18.45ms/拷贝），支撑离线 Oracle 与 MPC 预测器校准。

**形态 B：多进程真实栈（2026-10-21，模式 1 壁钟）**。本机多进程运行原始 Dynamo frontend（真实 HTTP OpenAI API）→ 原始 Dynamo router（file 发现、ZMQ 事件面，免 etcd/NATS）→ N 个原始 vLLM worker 进程（AsyncLLM/EngineCore/Scheduler 原码，真实 HTTP/TCP/ZMQ 通道）。唯一注入点：SimExecutor 经 vLLM `Executor` 限定名注入替换模型执行为 `time.sleep`（vLLM 源码零修改，包装层补齐 0.20.2↔0.28 两处运行期 API 漂移）；远端 KV 由 storage-sim 服务承担（真实 KVConnector 语义的远端前缀命中）。`run_stack_experiment.sh` 一条命令完成起栈→回放→验收断言→必定停栈。

**两形态分工**：DES 管定量对照（µs 确定性、CRN 两臂、守恒断言），进程栈管协议正确与在线路径（真实服务拓扑上的 admission 投放点、StepBatch 事件流观测）。

### 2.2 SimLLM：框架在环（framework-in-the-loop）+ 包级网络

管线：workload 模型 → 真实框架调度器（vLLM v0.26.0 经 `--distributed-executor-backend` 接入 SimExecutor，零 fork；SGLang 经插件接入 SimTpModelWorker）→ 仿真 GPU executor（roofline / profile 表 / trace 驱动的计算耗时）+ 设备模型（硬件调度器 SM/warp、HBM、copy engine、PCIe、NVLink scale-up 端口，"GPU 和 NIC 都是有端口的盒子"）→ 分组级包级网络（htsim 的 RNIC 模型族 htsim_rnic / rnic-nn / rnic-nn-fluid / rnic-cn，DCQCN 等 CCA；ATLAHS/GOAL 工具链；Clos fabric）→ 虚拟时钟上的 TTFT/TPOT/良好吞吐/SLO。RNIC 内部拆为 CCA、PCIe 引擎/DMA、QPC 三个可插拔子系统（signal-slot 事件总线）。耦合两模式：开环（把全部调度步录成一条 GOAL 依赖图 trace 一次仿真）与闭环（每步仿真完成时间推进调度器看到的虚拟时钟，拥塞改变组批、组批改变流量）。

里程碑现状（README，2026-10）：M0–M3 已落地（含 m4：live vLLM engine TP=8 下 36 项预注册检查全过）；M4 闭环运行时、M5 MoE 标定、M7 RNIC 模块进行中；**M6 PD 分离/KV 传输尚属待办**；KV 生命周期与 KV 传输流量是公开缺口（当前仅真实框架的前缀命中记账 + CPU pre-play oracle 离线固定输出长度/停止原因/专家路由）。训练在仓库描述中，README 内容以 serving 为主。

### 2.3 llm-emu：profile 驱动的单实例在线仿真

思路极简：真实 GPU 上一次性 profiling（`trace_profiler.py`）→ 构建profile 包（`serving-full.json`）→ 在 CPU 主机上照常 `vllm serve`，插件（`vllm.platform_plugins` 入口点）经 executor hook 拦截 `execute_model()`，把 GPU 前向替换为按预测延迟到期的 Future；密度感知的步延迟 oracle（自适应 K 的 Shepard 池化插值）根据每步 batch 构成给出延迟。除前向延迟外，scheduler、KV 管理、准入、tokenizer、HTTP 全部是 vLLM v0.18.1 未改逻辑（补丁仅 ~170 行、3 个文件，加 flag/接线不改语义；`cuda_mock` 提供 CPU 模式下的 torch.cuda 桩）。realtime 模式按预测值 sleep（对外是可压测的真实 HTTP 服务）；accelerated 模式跳过 sleep（未充分测试）。精度：六个复现格（M-Q8/M-Q14/A40-Q8/M-Q8-Burst/A40-L8/A40-Q4）误差多在 ±5% 内，突发负载 TTFT 最差 ~±10%。

## 3. 真实/仿真边界逐层对照

| 层 | 本仓库（DES + 进程栈） | SimLLM | llm-emu |
|---|---|---|---|
| 请求入口 | DES：trace JSONL；栈：**真实 Dynamo frontend HTTP OpenAI API** | workload 模型（到达、长度、共享前缀） | **真实 vLLM HTTP 服务**（可用真实压测工具） |
| 路由/分发 | **真实**：Dynamo router（栈）/ Rust SelectionService（DES）；记账 reserve/prefill-complete/free | 无路由层（单框架实例） | 无（单实例） |
| 调度器 | **真实 vLLM v1 Scheduler 0.20.2**；可选真实 vllm-ascend BalanceScheduler | **真实 vLLM 0.26.0 / SGLang 调度器** | **真实 vLLM 0.18.1 调度器** |
| KV 管理 | **真实 KVCacheManager/前缀缓存** + 真实 KVConnectorBase_V1 接口对接仿真存储 | 真实框架前缀/KV 记账；KV 传输流量未建（M6 待办） | 真实（但无远端 KV 概念） |
| 模型执行 | 仿真：SimExecutor `time.sleep`（栈）/ 线性系数（DES） | 仿真：计算耗时提供者 + GPU 设备模型 | 仿真：profile oracle 预测延迟 |
| GPU/加速器微架构 | 无（线性聚合系数） | 有（SM/warp 调度、HBM、copy engine、NVLink 域 A100/H100） | 无（黑盒步延迟） |
| 网络fabric | 无 | **核心**：包级 RNIC + Clos/蜻蜓拓扑 | 无 |
| 远端 KV 存储/IO | **核心研究对象**：ASU/Path 带宽模型、双策略、共享链路饱和与守恒审计 | 无独立存储模型（KV 传输=M6 待办） | 无 |
| 时钟 | DES 虚拟时钟（确定性）；栈壁钟 | 虚拟时钟（开/闭环） | 壁钟（realtime sleep） |
| 分布式并行 | 逻辑多 worker、单 rank；TP/PP 未实现 | TP all-reduce/P MoE all-to-all/PP 激活均在网络层建模 | 仅 uniproc executor |

要点：**三者在"调度器必须真实"上一致，但保留的真实栈厚度不同**——llm-emu 保留到 HTTP 入口，SimLLM 保留到框架引擎并向下建网络，本仓库独有地保留了**实例之上的路由层与多进程服务拓扑**（frontend→router→workers），这正是 KV 感知路由、准入控制、MPC 投放点研究所需的最小真实集。

## 4. 时间模型与耦合方式

- **SimLLM**：虚拟时钟，开环/闭环双模式。闭环即"仿真完成时间反馈进真实调度器"，与本仓库 DES 的做法同构（本仓库存储异步完成事件驱动真实 Scheduler 下一步决策，天然闭环）。差异在反馈的物理内容：SimLLM 反馈的是网络+计算耗时，本仓库反馈的是**远端存储 IO 完成时间**。
- **llm-emu**：壁钟。realtime 模式让被仿真进程成为一个时间比例尺 1:1 的真实服务端，优点是可用真实客户端/压测工具做端到端实验，代价是实验时长=真实时长、无确定性。
- **本仓库**：刻意双形态。DES 虚拟时钟承担 µs 级时序保真、确定性复跑、深拷贝重放（Oracle/预测器校准前提）；进程栈壁钟承担协议正确性与在线路径真实性（模式 1），虚拟时钟服务化（模式 2）设计上后置。这是三家中唯一同时持有"确定性离线"与"真实在线"两种时间形态的。

## 5. 保真度目标与校准方法

三家对"保真"的定义处于不同层，直接比误差数字没有意义：

1. **机制保真（本仓库）**：真实代码语义即 ground truth——真实 SelectionService 记账、真实 vLLM 调度/前缀/KVConnector 语义、真实 Dynamo 进程间协议。绝对时间系数是**工况绑定量**（如 A/B 实验按 E26 大纲以"每类总读字节+总计算时间"标定，虚拟 token 化并公示偏差），明确声明非任何 NPU 型号实测。验收方法学：不伪造结果（VALIDATION.md 只录真实运行输出）、源码 SHA+指纹校验+`doctor.py` 溯源、守恒断言（∫实际速率=读字节数）、CRN 两臂对照、Linux/macOS 五模式逐位一致、深拷贝轨迹逐位一致。
2. **网络物理保真（SimLLM）：** 分层验收——闭式解复现（m1/m5 "0 ps residual"）、live vLLM TP=8 预注册 36 检查、对真实 NIC 锚点（UCCL、DCQCN、HPCC、Kalia）与真实 GPU（A100、GH200）标定，已知缺口公示（如 DCQCN 在 64–256KB 欠冲 0.79×）。**预注册研究**（expectations.md 先冻结、RESULTS.md 带证据审计、未能预注册者标注 post-specified）是其方法学特色。第三方评述（知乎 2026-08）指出其绝对物理时间层（真实 GPU 端到端）标定仍在推进（compute_fidelity_v1 标记 VOID：固定每步开销为已建模 decode 计算的 1.79–12.31×）。
3. **单点绝对时间保真（llm-emu）**：六格复现，TTFT/TPOT/ITL/E2E/TPS 误差多 <±5%（最差 ~±10%），条件是 (GPU, 模型, 量化) 三元组有对应 profile 包（首次 GPU profiling 约数小时壁钟）。

## 6. KV 与存储建模（本仓库的主场）

- 本仓库把**远端共享 KV 存储当作第一公民研究对象**：块哈希唯一落盘（ASU）、Path 为 IO 发放时的访问通道、`active_split`（FIFO 排队+活跃分裂速率）与 `max_min_fair`（带需求上限的水土填充到不动点、无排队）双策略、共享链路等比缩放与饱和、`storage_interval` 事件流与守恒审计。A/B 双类争抢实验证明真实 vLLM 异步 KV 加载语义下"错峰无法靠到达序获得"——这类结论只有存储 IO 进了仿真闭环才做得出来。
- SimLLM 当前只有框架内 KV/前缀记账；KV 生命周期与 PD 间 KV 传输流量（M6）待办——恰是本仓库已跑通的部分（storage-sim 远端 KV 命中已在真实 KVConnector 语义下验收，慢存储对照 9.2×）。
- llm-emu 无远端 KV 概念（单实例 HBM KV 由真实代码管理）。

## 7. 工程侵入性与版本治理

| | 接入缝 | 上游修改 | 版本策略 |
|---|---|---|---|
| 本仓库 | vLLM `Executor` 限定名注入（进程栈）；进程内直接构造真实对象（DES） | **vLLM 零修改**（macOS 上 setup.py 一行适配另行登记）；Dynamo 从源码带 feature 编译；`patched/` 薄补丁留档 | 三个上游按 `versions.json` SHA 固定 + `source-fingerprints.json` 校验 + `doctor.py`；升级需同步两文件并重跑验收 |
| SimLLM | vLLM `--distributed-executor-backend` 插槽（零 fork）；SGLang 插件 | 无 fork | vLLM 钉在 v0.26.0 |
| llm-emu | `vllm.platform_plugins` 入口点 + executor hook | ~170 行 / 3 文件补丁（或 drop-in 覆盖） | vLLM 钉在 v0.18.1（指定 commit），换版本需重 basing |

三家都钉 vLLM 版本但取向不同：llm-emu 0.18.1（旧、为了补丁稳定）、本仓库 0.20.2（为 dynamo.vllm 兼容与 vllm-ascend 0.20.2rc1 对齐）、SimLLM 0.26.0（新、为了 executor 插槽成熟）。vLLM RFC #61305 把这类外部接入统称为"fragile hooks"，主张树内标准化仿真缝（pluggable TimingProvider + real/fast-forward/virtual 时钟）——若落地，三家都是受益者，接入维护成本都会下降。

## 8. 规模、平台与运行成本

- **SimLLM**：目标 4+、尤其 64+ 节点集群；参考拓扑 8×8 B100（64 GPU、每 GPU 400G NIC、两层 Clos）。代价：C++ 工具链 + ~250MB 子模块 + CMake 构建；CPU 主机即可跑仿真（这是其价值主张）。
- **llm-emu**：单 GPU 实例；CPU 主机运行时零 GPU 开销，但需要一次性真机 profiling；实验时长=真实时长（realtime）。
- **本仓库**：单机 CPU（笔记本可跑）；DES 32 逻辑 worker；进程栈 2 worker=6 进程、空闲 RSS 合计 ~0.3–0.5GB、8 请求全栈 CPU 时间增量 ~0.09s、跑完即停。无 GPU/NPU/CANN/etcd/NATS 依赖。

## 9. 各自不可替代的问题域

**只有本仓库能做**：
- 真实 Dynamo 路由语义下的 KV 感知路由/准入策略研究（真实 reserve/free 记账、真实 ZMQ 事件面）；
- 共享远端 KV 存储的带宽争抢与分配策略（ASU/Path、max-min fair、共享链路）对调度的影响；
- vllm-ascend 真实调度代码（BalanceScheduler）的策略实验与补丁验证；
- MPC 在线调度的三件套研究环境：admission 投放点（栈）、StepBatch 事件流观测、可深拷贝逐位重放的影子世界（DES）做离线 Oracle 与预测器校准；
- S0-vs-在线-vs-Oracle 三对照验收场。

**只有 SimLLM 能做**：
- 采购/规划期的部署前预测与 Pareto 扫描（拓扑×TP 切分×负载）；
- incast/队列堆积/CCA 振荡/自适应路由等网络机制对服务指标的影响归因（其 dcqcn_vs_cn 实验：缓冲超限时 DCQCN p99 恶化 1161× vs CN 1.60×）；
- MoE 专家并行 all-to-all 流量与 NVLink 域建模。

**只有 llm-emu 能做**：
- 无 GPU 条件下对特定 (GPU, 模型, 量化) 组合的 ±5% 级指标复现与容量规划；
- 用真实压测客户端打真实 HTTP 服务端的在线实验。

**三家的公开缺口互为镜子**：SimLLM 缺 KV 传输/路由层（本仓库已有）、缺绝对时间标定收口（llm-emu 最强）；llm-emu 缺多实例/网络/存储（单实例单卡）；本仓库缺 GPU/网络物理保真与集群规模（SimLLM 强项）。

## 10. 共同趋势：executor 仿真缝正在被标准化

[vLLM RFC #61305](https://github.com/vllm-project/vllm/issues/61305)（simulated execution with pluggable timing models，基于 PR #47922"真调度器无前向运行"）提出：`execute_model/sample_tokens` 之上全部不改，worker 步替换为可插拔 TimingProvider（zero/constant/table k-NN/replay + 限定名外挂），时钟 real/fast-forward/virtual 三态。RFC 点名 SimLLM 用自定义 executor backend、Dynamo mocker 重实现调度器易漂移等"脆弱钩子"问题。本仓库的 SimExecutor 限定名注入正是同一缝的自建版；RFC 落地后（预计新版本 vLLM），本仓库可评估迁移到树内缝以消除 0.20.2↔0.28 API 漂移垫片。

## 11. 对本仓库的启示（可选借鉴，不改变现有定位）

1. **预注册实验方法学**（SimLLM 的 expectations.md 冻结→RESULTS.md 审计→post-specified 标注）：与本仓库"不伪造结果"规则同源但更进一步，可考虑在正式对照实验（E26–E31、A/B 系列）中引入 expectations 冻结文件。
2. **profile 包式标定**（llm-emu）：本仓库计算系数目前为工况绑定/合成值；未来若需绝对时间可信度，可一次性在真实 NPU 上采集"每步延迟 vs batch 构成"的 profile 包替代线性系数，而不必引入设备微架构模型。
3. **关注 RFC #61305 的 TimingProvider 落地**：树内仿真缝成熟后可降低对 vLLM 版本钉死的维护成本（本仓库 0.20.2 钉死主要受 dynamo.vllm 与 vllm-ascend 对齐约束，需综合评估）。
4. SimLLM 的**开环 GOAL trace 单次回放**思路（先录全部调度步、再一次仿真）与本仓库 DES 深拷贝重放目标相似，其"闭式解 0 残差"验收粒度值得借鉴到存储带宽策略的单元验收。

## 12. 参考资料

- SimLLM：[仓库](https://github.com/openfabric-systems/simllm)（README 抓取于 2026-10-21）；知乎评述《[欢迎 SimLLM！大模型部署仿真终于等来真正的端到端闭环](https://zhuanlan.zhihu.com/p/2072254703203792786)》（2026-08-16，第三方，反映 2026-08-15 时点仓库状态）
- llm-emu：[仓库](https://github.com/AKafakA/llm-emu)；论文《[LLM-Emu: Profile-Driven Native Online Emulation of LLM Serving Systems](https://openreview.net/forum?id=9jrjM8ZQyn)》（MLArchSys@ISCA 2026 poster）
- vLLM RFC：[#61305 Simulated execution with pluggable timing models](https://github.com/vllm-project/vllm/issues/61305)
- 相关同族工作（背景参考）：[LLMServingSim](https://github.com/casys-kaist/LLMServingSim)（IISWC 2024，HW/SW 协同仿真）、[llm-d-inference-sim](https://github.com/llm-d/llm-d-inference-sim)、[Vidur](https://github.com/microsoft/vidur)（Microsoft）、[ReaLLM](https://github.com/bespoke-silicon-group/reallm)（ASAP 2025）
- 本仓库：`AGENTS.md`、`dynamo_vllm_cpu_sim/README.md`、`docs/多进程真实Dynamo-vLLM仿真栈设计-20261020.md`、`docs/MPC调度架构前提与真实Dynamo-vLLM落地挑战-20261010.md`、`docs/存储带宽分配策略设计-maxmin-20261009.md`、`AB双类带宽争抢实验结果-20261008.md`

*注：SimLLM 与 llm-emu 的信息均来自其公开 README/论文页/issue 的 2026-10-21 抓取快照；两项目均在快速迭代（SimLLM M4–M7 进行中），横向对比结论随时间推移需复核。*
