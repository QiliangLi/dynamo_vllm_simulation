# agentx_random_360_12s_12_180GBps


固定360条AgentX请求，12秒到达，180GB/s单共享带宽池，12个逻辑实例。输入为2026-10-30已验收三方案实验的原文件，没有重新抽样或修改命中率。

|项目|值|
|---|---|
|选取位置|抽中40主链组后，按全局请求序号30%位置起取第2608—2967条|
|输入token|23,255,872|
|有效远端命中token|22,588,592，97.130703%|
|新计算token|667,280|
|累计KV读取|2,480,769,527,808 B，2480.769528 GB|
|资源|12实例；K1；block16；30159逻辑块；每步最多4条/8192新token|
|计算|glm5_direct_sum_v1；launch_saving_fraction=1；TP1/DP8/EP8参考画像|
|执行|78层流水；关闭本地prefix caching；EDF；每请求输出1 token|
|方案|直接准入、TFS α=0.1、TFS α=1；TFS首token释放，关闭强制准入|
|反馈|关闭四类在线执行反馈；原生Dynamo最终答复及后台EOF收尾|
|预热与SLO|72条预热，排空和历史/GC重置后360正式；期限=arrival+4×参考服务时间|

原始数据源为`semianalysisai/cc-traces-weka-062126`，固定revision和抽样种子416见manifest。360条保留原相对间隔并缩放到0～12秒，不是均匀每秒30条，也不是12秒完成承诺。

命中来自完整主链组及子代理历史`hash_ids`的转换结果。有效远端前缀按block16取整，并至少保留一个新块。假设窗口前前缀已经在远端，非片段冷启动或实测存储命中。合成token只承载长度，不复现工具调用和Agent依赖。A90/B270是整段输入外读压力P75分组，不是在线TFS重读标记。

## 文件与导出

- `trace.jsonl.gz`：原实验JSONL逐字节压缩，含完整token数组、时间和命中来源。
- `requests.csv`：360条可读明细，包括源行号、到达、h/u和读取量。
- `config.json`、`config.historical.json`：直接准入默认配置，已移除本机Dynamo overlay路径；原配置SHA保留在manifest。
- `run_contract.json`：三个方案的完整配置及运行合同。
- `manifest.json`：来源、计数、模型边界和文件SHA256。

从仓库根目录执行，输出目录必须不存在：

```bash
python3 benchmark/manage.py verify
python3 benchmark/manage.py export agentx_random_360_12s_12_180GBps --output temp/agentx360_input
```

导出包含`trace.jsonl`、`config.json`和`run_contract.json`，不会启动仿真。解压后SHA256为`eeafecc40e2eb2e5185875e872716056531daecb13bcb768fa9555efacf2e707`，与原验收实验完全一致。

## 运行与验收

2026-11-02：统一交付运行器已接入本负载。状态为 `supported`，可按名称或旧别名
`agentx360_12s_180gb` 运行，也纳入 `--case all`。六秒 synthetic 负载仍只支持校验和导出。
依赖准备与原生加权Dynamo构建见仓库 `docs/实验分支使用说明.md`。

在仓库的 `dynamo_vllm_cpu_sim/` 目录执行。每次指定新的输出目录：

```bash
.venv/bin/python scripts/run_benchmark.py \
  --case agentx_random_360_12s_12_180GBps --arm tfs-a01 --smoke \
  --dynamo-python build/weighted-dynamo/python \
  --output results/benchmark_runs/agentx-smoke-r1

.venv/bin/python scripts/run_benchmark.py \
  --case agentx_random_360_12s_12_180GBps --arm all \
  --dynamo-python build/weighted-dynamo/python \
  --output results/benchmark_runs/agentx-r1
```

`--arm` 支持 `direct`、`tfs-a01`、`tfs-a1` 和 `all`。冒烟用2实例，各展示类别取最短、
中位、最长请求，共6条，包含300480 token请求，不预热，计划时刻置零。正式运行每组
12实例、72预热和360正式请求。三组串行执行，保留失败输出，拒绝覆盖目录。

`run_contract.json` 保留三组原始配置，并明确声明运行期覆盖：
`compute.context_length_limit=300481`。运行器只对该profile显式注入此值。
原始trace、requests.csv、config.json、config.historical.json以及三组原配置未改变。
程序不修改冻结公式文件或全局MODEL，也不在运行时打源码补丁。

合同另声明网关启动就绪预算180秒，用于长输入的分词往返检查、画像准备和服务启动。
此预算只作用于请求发送前的启动阶段；运行期额度RPC仍为5秒，存储和请求超时未放宽。
首次正式尝试触发旧40秒启动上限；第二次完成全部432请求，但新增配置验收发现启动器
改写了直接准入下已禁用的waiting_factor。两次记录均保留。当前启动器保留该原始值，
三方案完整配置逐项核对，不以字段“不生效”为理由跳过合同验收。

39条请求超过原公式范围131072 token，最长300480。长度检查的显式放宽仍是**公式外推**，
不是GLM5长上下文实测定标。未显式配置时，普通负载继续使用131072上限；超出显式
上限也会失败。计时事件保留实际长度、许可上限和外推标记，独立公式检查同时核对标记。

启动器冻结源码、输入、有效配置及原生构建SHA。正式验收核对：

- 三组完整有效配置与合同一致；12秒到达、180GB/s、K1、EDF及直接组合模型不替换。
- HTTP正式请求ID与AgentX账本ID完全一致；360条、h/u、累计读取字节和期限一致。
- 72预热覆盖12实例，排空后清除分类历史，重置正式GC窗口。
- 最长300480 token确实进入计算；公式、逐层KV屏障、实际slot/token预算、TFS许可和评分验收通过。
- 在线执行反馈为零；执行事实由本地日志离线合并；原始在线快照另存。
- Dynamo最终答复及后台EOF全部排空；配额和路由账目归零；所属进程退出。
- Kahan补偿求和只改善TB级累计诊断字节精度，不改变读取分配、速率和完成时刻。

验收摘要见 `dynamo_vllm_cpu_sim/AGENTX_VALIDATION.json`。本次验证使用WSL/Linux、固定依赖
和已有原生构建，没有重装全部依赖。Windows原生多进程运行及真实GPU/NPU未验证。
三方案各执行一次，只证明当前入口通过功能验收，不保证重现旧报告的时延排名。
原始运行事件留在本机，不随benchmark发布。当前运行摘要输出在 `summary.csv` 和
各组 `run/benchmark.check.json`，历史报告仍描述当时的冻结版本。
