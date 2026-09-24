# Ascend路径问题说明

**User:** Anonymous  
**Created:** 9/24/2026 11:50:20  
**Updated:** 9/24/2026 17:17:26  
**Exported:** 9/24/2026 18:58:22  
**Link:** [https://chatgpt.com/c/6ab49df4-079c-83ec-90ef-ee9155dbc1c3](https://chatgpt.com/c/6ab49df4-079c-83ec-90ef-ee9155dbc1c3)  

## Prompt:
2026/9/24 11:50:11

我想搭建一个仿真环境，但是调度器和推理引擎层都用真实的代码，调度器就用真实的Dynamo的代码，推理引擎层就用真实的vllm代码（包括vllm-ascend适配，vllm的版本是v0.20.2，vllm-ascend的版本是v0.20.2rc1）。仿真环境就运行在CPU上，对于需要NPU实际跑的，或者需要接存储的，全部mock成仿真的代码。这样对于调度策略的修改（例如考虑存储状态），完全是基于真实代码进行的，相比于直接搭一个仿真框架，更不易失真。你考虑一下这个方案的可行性，以及如果能干的话应该怎么干？给一个详细的分析和设计方案。我希望能根据你给的这个代码能直接搭。

## Response:
2026/9/24 11:50:21

我会先核对 Dynamo、vLLM v0.20.2 和 vLLM-Ascend v0.20.2rc1 的源码与接口，确定哪些调度逻辑能直接保留、哪些硬件调用需要替换，再给出设计方案和可搭建的代码骨架。

已经确认一个关键点：Dynamo 自带的 Mocker 会模拟推理引擎调度，不能直接满足你“运行真实 vLLM 调度代码”的要求。方案需要保留 Dynamo 的路由代码和 vLLM 的调度、KV 管理代码，把模型执行与数据搬运接到仿真后端。我也会使用 Library 文件技能，把完整方案和启动包整理为可下载文件。

源码里找到了两个可行的切入点：vLLM 官方已有 CPU 上运行真实 Scheduler 的单元测试；Dynamo v1.5.0 提供了可独立调用的路由选择核心。这样第一版可以直接测试真实决策代码，不必先启动完整服务集群。另外，Ascend 的适配会修改部分调度行为，需要明确启用了哪些补丁，不能只安装插件就声称完成了 Ascend 仿真。

Dynamo 的真实路由核心已经在 CPU 上完成了 worker 注册、请求选择和负载释放。接下来验证最关键的闭环：远端 KV 命中后，真实 vLLM Scheduler 是否会进入等待状态，并在仿真 I/O 完成后继续执行。这个闭环决定了后续能否可信地研究存储状态对调度的影响。

启动包会明确区分“已跑通的基础闭环”和“后续研究扩展”。基础闭环包含真实 Dynamo 路由、真实 vLLM 调度、Ascend 的指定调度代码，以及共享存储竞争模型；逐层加载、PD 分离、完整 KV 事件同步和 NPU 性能校准会在设计中单独展开，避免把未实现的行为算进仿真结果。

基础闭环已跑通：12 个请求全部完成，远端 KV 的仿真读取实际进入了 vLLM 的等待/恢复流程，结束后 KV 引用和 Dynamo 请求负载都已释放。现在正在验证 Ascend 调度路径和重复回放的一致性；示例中的计算耗时参数仅用于验证流程，不能当作 NPU 性能数据。

Ascend 路径验证发现了一个具体问题：在启用非默认的 `BalanceScheduler` 并配合异步 KV 加载时，该版本有一处状态更新与 vLLM 主线不同，在当前仿真连接器下会反复加载、无法推进。我保留了原始源码，另外提供一行补丁和复现说明；补丁后的 CPU 回放已通过，但还不能视作经过 NPU 验证的修复。

## Prompt:
2026/9/24 16:50:52

继续。

## Response:
2026/9/24 16:51:05

我继续完成启动包和验证记录。接下来确认慢存储是否会拉长真实调度路径中的等待时间，并从干净环境复跑安装步骤，检查你解压后能否直接启动。

这次继续时运行环境已重建，之前尚未保存的临时文件需要恢复；源码核对结论和已完成的验证结果都还在。我会恢复工程后再复跑关键路径，并把方案与代码一起交付。

源码和依赖已恢复。启动包会明确区分“真实调度逻辑”和“仿真执行”：Dynamo 负责真实的路由选择，vLLM/Ascend 负责真实的请求与 KV 状态变化，计算耗时和存储完成事件由仿真层驱动。

默认路径已经重新跑通，远端 KV 会经过真实的等待、接收和恢复调度状态。Ascend 的非默认 balance 路径需要单独处理：我发现其异步 KV 分支少更新了一个计数，代码包会提供显式启用的补丁，并保留未修改的默认路径。

慢存储对照也跑通了：共享链路从 10 GB/s 降到 0.1 GB/s 后，平均首 token 时间从约 5.36 ms 增至 252.65 ms，等待会实际影响调度结果。这些是验证因果链路的合成数据，不是 NPU 性能预测；后者还需要实机标定。

全新 Python 虚拟环境的安装、启动和结果检查已通过。方案中还会说明一个关键限制：当前闭环使用 Dynamo 的真实选择核心，但尚未接入 vLLM 的 KV 事件流，所以默认路由明确关闭缓存命中加分；缓存感知路由需要先补齐这条状态反馈链路。

详细方案已写完，包含真实代码与仿真层的边界、存储竞争模型、Dynamo/Rust 和 vLLM 策略修改入口，以及逐层重叠、PD 分离和完整 EngineCore 的扩展步骤。最后正在用不含源码和虚拟环境的目录验证下载启动流程，完成后交付方案和代码压缩包。

从空目录下载固定版本源码并启动的复验也通过了。两份交付物已准备好：约 2.1 万字的详细设计，以及包含安装脚本、仿真代码、配置、测试和验证记录的代码包。代码包不包含大型依赖或模型权重，首次启动会下载所需依赖。

正在保存详细方案和可运行代码包。

> **Checking tests and artifacts**
>
> **Checking tests and artifacts**
>
> **检查测试与构建产物**
>
> 
>
> **汇总推理基准测试结果**
>
>
> **汇总基准测试并检索工作区Dynamo文件**
>
> 
>
> **Recovering files, saving checkpoint**
>
> **Recovering files, saving checkpoint**
>
> **恢复文件并保存检查点**
>
> 
>
> **搜索并核实 Dynamo 与 vLLM 的 CPU 支持**
>
> 
>
> **检查项目环境与依赖路径**
>
>
> **检查项目环境与依赖路径**
>
> 
>
> **下载并整理上游项目源码**
>
>
> **配置Dynamo vLLM CPU模拟环境并应用代码补丁**
>
> 
>
> **查看模拟引擎与路由存储代码**
>
>
> **完成仿真引擎检查、vLLM安装、代码修改及调度测试**
>
> 
>
> **检查日志并定位工作节点选择代码**
>
>
> **Preparing runnable core scaffolding**
>
> **Preparing runnable core scaffolding**
>
> **Checking engine timing invariants**
>
> **Checking engine timing invariants**
>
> **准备可运行核心脚手架并检查引擎时序不变量**
>
> 
>
> **Preparing runnable core scaffolding**
>
> **Preparing runnable core scaffolding**
>
> **Checking engine timing invariants**
>
> **Checking engine timing invariants**
>
> **准备可运行核心脚手架并检查引擎时序不变量**
>
> 
>
> **检查调度日志并运行多种策略模拟测试**
>
> 
>
> **分析基准结果并检查关键实现**
>
>
> **Fixing integration issues**
>
> **Fixing integration issues**
>
> **修复了集成问题**
>
> 
>
> **检查结果并搜索路由配置**
>
>
> **Adding source fingerprints**
>
> **Adding source fingerprints**
>
> **添加了来源指纹**
>
> 
>
> **检查路由实现、配置并运行测试脚本**
>
> 
>
> **检查安装日志与代码引用**
>
>
> **检查安装日志、代码引用及 Ruff Black 配置**
>
> 
>
> **自动审核已停止**
>
> 
>
> **检查 PyPI 软件包可访问性**
>
>
> **Setting download proxy**
>
> **Setting download proxy**
>
> **Configuring bootstrap environment**
>
> **Configuring bootstrap environment**
>
> **设置下载代理并配置引导环境**
>
> 
>
> **编写 Dynamo 与 vLLM 调度仿真启动包**
>
>
> **Bootstrap succeeds**
>
> **Bootstrap succeeds**
>
> **Testing fresh source bootstrap**
>
> **Testing fresh source bootstrap**
>
> **测试全新源码引导流程成功**
>
> 
>
> **Bootstrap succeeds**
>
> **Bootstrap succeeds**
>
> **Testing fresh source bootstrap**
>
> **Testing fresh source bootstrap**
>
> **验证全新源码引导并确认成功**
>
> 
>
> **检查依赖可访问性并完成调度仿真代码整理**
>
> 
>
> **概述存储感知路由策略设计**
>
>
> **Validating physical model configs**
>
> **Validating physical model configs**
>
> **验证了物理模型配置**
>
> 
>
> **查看运行脚本与库上传工具**
>
>
> **Checking actual code scope**
>
> **Checking actual code scope**
>
> **检查了实际代码范围**
>
> 
>
> **搜索输出块函数与解码块引用**
>
>
> **Preparing validation documentation**
>
> **Preparing validation documentation**
>
> **编写验证文档**
>
> 
>
> **更新指纹校验与仿真结果验证**
>
>
> **完成存储感知路由开发、校验仿真并记录运行日志**
>
> 
>
> **Waiting for validation tests**
>
> **Waiting for validation tests**
>
> **Preparing validation artifact**
>
> **Preparing validation artifact**
>
> **准备验证材料并等待验证测试**
>
> 
>
> **查看清理安装日志末尾**
>
>
> **Checking bootstrap exit codes**
>
> **Checking bootstrap exit codes**
>
> **Reviewing bootstrap troubleshooting**
>
> **Reviewing bootstrap troubleshooting**
>
> **检查引导退出代码并查看故障排除指南**
>
> 
>
> **更新常见问题并验证代码**
>
>
> **Copy reports and validate sources**
>
> **Copy reports and validate sources**
>
> **Drafting standalone quickstart**
>
> **Drafting standalone quickstart**
>
> **复制报告并验证来源，起草独立快速入门指南**
>
> 
>
> **查看安装日志并验证代码更新**
>
> 
>
> **打包并完善 CPU 仿真工程**
>
>
> **完善并打包CPU仿真工程**
>
> 
>
> **上传 Dynamo vLLM 文件到库**
>
>
> **上传 Dynamo vLLM 文件至库**
>
> 
>
> 思考了 25m 42s

**方案可行。我已经完成详细设计，并做了一个可直接启动、经过验证的 CPU 调度仿真原型。** 核心原则是保留真实调度决策和状态机，用有延迟、排队及资源竞争的事件模型替换计算与存储。

交付文件：

- [Dynamo_vLLM_CPU_Simulation_Design.zh-CN.md](sandbox:/workspace/scratch/d6236fc2f388/Dynamo_vLLM_CPU_Simulation_Design.zh-CN.md)：详细设计，包含源码入口、版本兼容、存储建模、策略修改方法和后续实施步骤。
- [dynamo_vllm_cpu_sim.zip](sandbox:/workspace/scratch/d6236fc2f388/dynamo_vllm_cpu_sim.zip)：安装脚本、仿真代码、配置、测试、Ascend 可选补丁及原始验证记录。

当前代码的实现范围如下：

| 部分 | 实现 |
|---|---|
| Dynamo 路由 | 真实 Rust `SelectionService`，包含选择和负载预留 |
| vLLM v0.20.2 | 真实 Scheduler、Request、KVCacheManager、块分配与状态更新 |
| Ascend v0.20.2rc1 | 加载真实 `BalanceScheduler` 文件；提供默认路径和显式补丁实验路径 |
| NPU 执行 | 按虚拟完成时间生成 `ModelRunnerOutput` |
| 外部存储 | 模拟路径排队、磁盘带宽、共享链路和异步 KV 接收 |

你未指定 Dynamo 版本，我选用了 **v1.5.0**。它的官方发行组合采用 vLLM 0.28.0，因此这里通过独立选择核心桥接指定的 vLLM 0.20.2，不能将其视为官方整套引擎适配的兼容组合。([docs.dynamo.nvidia.com](https://docs.dynamo.nvidia.com/dynamo/reference/releases/v1-5-0?utm_source=chatgpt.com))

在 Linux x86_64 上安装 `uv` 后，执行：

```bash
unzip dynamo_vllm_cpu_sim.zip
cd dynamo_vllm_cpu_sim
bash scripts/bootstrap.sh
```

我已从**不含源码和虚拟环境的新目录**验证这套启动流程。脚本会下载固定提交、安装 CPU 依赖并运行示例，无需 NPU 或模型权重。

验证中，12 个请求均完成，KV 块可回收，Dynamo 负载预留归零。共享存储链路从 10 GB/s 降为 0.1 GB/s 后，平均首 token 时间由约 **5.36 ms 增至 252.65 ms**，说明存储等待确实进入了真实调度闭环。这些数值是合成模型的验证结果，不能当作 NPU 性能预测。

**当前原型的范围是调度核心。** 完整 EngineCore、全量 Ascend 插件、Dynamo KV 事件桥、逐层 I/O 重叠和 PD 分离仍需继续实现。文档分别给出了这些部分的接入位置和验收条件；默认路由明确关闭缓存命中加分，避免在缺少真实缓存反馈时产生误导。

---

**Sources:**

- [Dynamo v1.5.0](https://docs.dynamo.nvidia.com/dynamo/reference/releases/v1-5-0?utm_source=chatgpt.com)



---
Powered by [ChatGPT Exporter](https://www.chatgptexporter.com)