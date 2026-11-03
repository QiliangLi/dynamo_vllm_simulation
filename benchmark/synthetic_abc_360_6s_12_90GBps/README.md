# synthetic_abc_360_6s_12_90GBps


固定的人工 ABC 负载。来源是 E26f C_A、seed416 的原360条到达顺序，不是 Mooncake trace。
本负载已在研究版实际执行直接EDF、新TFS α0.1、新TFS α1；各360正式请求验收通过。
输入和历史配置保留2026-10-30的已验收版本。2026-11-02接入统一交付运行器，并重新执行启动及三方案功能验收。

## 请求与资源

|类别|条数|远端命中 token|新算 token|参考纯计算 ms|单请求读取 GB|
|---|---:|---:|---:|---:|---:|
|A|120|30720|259|118.658951|3.373793280|
|B|120|9392|2048|346.391576|1.031467008|
|C|120|0|348|118.674961|0|

12个逻辑实例、90 GB/s单共享池、K=1、block16、12258逻辑块、batch≤4/8192新token。
GLM5为78层，TP1/DP8/EP8，`glm5_direct_sum_v1`、启动合并比例1。
每token完整KV共109824字节。逐层加载并预取下一层，每请求外部前缀只读取一次。
关闭本地前缀缓存。相同请求形状也逐请求计算读取量，不跨请求免读。

总input为5132040 token，新算318600 token，读取528.631234560 GB。
参考计算70.047058468实例秒。6秒窗口的计算输入压力97.29%，存储输入压力97.89%。
这些是公式和输入量，不能当成实际设备利用率。人工类别不等于在线P75重读标记。
稳定P75为A的28.432691 GB/s，导出配额floor(90/28.432691)=3；初期配额可以更大。

## 到达时间与命中构造

保持原 seed416 的顺序，每条计划到达为 `original_seed416_arrival_s × 6 / 3.6119999999999997`。
时间原点是0，最后一次计划到达为6秒；首请求为0.005033375727249秒。
首末间隔为5.994966624272752秒。不能把首请求再平移到0，否则输入与实跑不一致。
`requests.csv` 的 `source_timestamp` 单位为秒，保留原冻结到达值；`source_line` 是原冻结JSONL行号。

各类h/u为固定合成条件，没有逐请求96%命中或全局4%预算约束。
保留真实运行的token数组、请求ID、顺序和计划时间。gzip解压后与研究实验输入逐字节相同。
JSONL中的T0和deadline是冻结计划参考；实际回放须按有效配置及实际到达重新计算。

## 校验与导出

从仓库根目录运行：

```bash
python benchmark/manage.py verify
python benchmark/manage.py catalog --check
python benchmark/manage.py export synthetic_abc_360_6s_12_90GBps --output temp/synthetic_abc_6s_input
```

默认`config.json`为已验收直接EDF的完整配置，去掉本机overlay路径。
三组实际配置完整保存在`run_contract.json`；TFS分别使用α0.1和α1，无强制准入或老化，按HTTP首token释放。
每组72预热、排空并重置分类历史与正式GC窗口。Runtime使用TCP/MessagePack，四类执行反馈关闭。

## 直接运行与验收

2026-11-02：状态为 `supported`，统一运行器支持本负载，并将其纳入 `--case all`。
先按仓库 `docs/实验分支使用说明.md` 准备固定依赖和独立加权Dynamo构建。
在 `dynamo_vllm_cpu_sim/` 目录执行，输出目录必须不存在：

```bash
.venv/bin/python scripts/run_benchmark.py \
  --case synthetic_abc_360_6s_12_90GBps --arm tfs-a01 --smoke \
  --dynamo-python build/weighted-dynamo/python \
  --output results/benchmark_runs/synthetic-abc-smoke-r1

.venv/bin/python scripts/run_benchmark.py \
  --case synthetic_abc_360_6s_12_90GBps --arm all \
  --dynamo-python build/weighted-dynamo/python \
  --output results/benchmark_runs/synthetic-abc-r1
```

`--arm` 为 `direct`、`tfs-a01`、`tfs-a1` 或 `all`。三方案共用EDF。
冒烟用2实例，每类取首尾各一条，共6条；计划时刻置零，不预热，仅检查正确性。
正式每组12实例、72预热和360正式，保留时间原点0及首请求之前约5.03ms间隔。
不能将首请求重新平移到0，也不能套用Mooncake的4秒或AgentX的12秒窗口。

运行器读取 `synthetic_abc360_v1` 专用合同，逐项核对三方案有效配置。
保留直接准入下已禁用的waiting_factor原值，不额外注入长上下文参数或启动超时覆盖。
最长30979 token在原131072范围内，不涉及AgentX长上下文公式外推。

完整验收包含：

- 360正式请求与HTTP账本ID一致；A/B/C各120条，固定h/u、累计token和528631234560 B读取一致。
- 原请求顺序与6秒计划到达值不变；T0和4×T0期限按当前配置及实际到达重算。
- 72预热覆盖12实例；确认排空，排除预热分类历史，重新开始正式GC窗口。
- 独立直接组合公式、逐层KV就绪屏障、原生token/slot预算和真实EDF排序通过。
- 最终P75压力约28.432691 GB/s，对应配额3；人工标签不代替在线分类，初期配额可以不同。
- 四种执行反馈保持关闭；原始在线快照与离线补全报告分开；Dynamo最终答复及后台EOF排空。
- 许可、路由记账和存储在途读取排空，所属进程退出，无资源保护触发。

小型验收摘要见 `dynamo_vllm_cpu_sim/SYNTHETIC_ABC_VALIDATION.json`。
当前统一入口验收与历史报告分开记录，不改写旧报告指标，不上传大型原始事件。
本次使用WSL/Linux、固定依赖及已有原生构建；不代表全新bootstrap、Windows原生进程或真实GPU/NPU验证。
三方案各一轮只证明功能通过，不证明稳定性能排名。

## 历史来源与边界

`provenance.json`、`validation.json` 保留加入输入时的研究实验凭证，不替代本次交付验收。
原输入、requests.csv、两份配置及三组历史方案配置未修改。解压trace的SHA256仍为
`3148c9e7cf8952f9c016ba9ac7e1db8993780d96bb2146f99d7abcfb01ec40b6`。
原研究DV提交为 `b6aca299ec9888d25435c94013f05d73fd8fa825`。
2026-10-30研究实验的首次直接组曾有1条Runtime返回流连接失败，失败轮保留；同参数重跑通过。
这条历史记录不表示本次验收也出现了同样失败。模型数值执行与KV传输仍为CPU解析仿真。
