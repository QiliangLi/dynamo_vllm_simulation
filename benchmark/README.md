# 固定Benchmark目录

原三个Mooncake case可由统一运行器执行。新增[agentx360_12s_180gb](agentx360_12s_180gb/README.md)固定输入与配置：360条、12秒、180GB/s共享带宽、12实例。AgentX用原完整历史命中，667280新计算token、2480.769528GB累计读取；运行合同为direct_sum及关闭在线执行反馈。

AgentX当前支持校验和导出，交付运行器尚未移植所需研究代码。显式选择该case会在启动前拒绝；`--case all`仍只运行原三个Mooncake case。不能把导出通过当作运行兼容。

```bash
python3 benchmark/manage.py export agentx360_12s_180gb --output temp/agentx360_input
```

`manage.py verify`现检查四份共1440条请求。AgentX配置独立，不受下文Mooncake共同90GB/s、4秒配置约束。

## 原三种Mooncake360 Benchmark

本目录保存三份已用于实验的固定负载。三份都取自
`conversation_trace.jsonl`前360条，保留原input长度、请求顺序及到达节奏，
将到达窗口归一化为4秒。不使用原output长度计算工作量，也不使用hash_ids推导命中。

|目录|构造规则|新计算token|实际新计算比例|读取量（GB，十进制）|类别数量|
|---|---|---:|---:|---:|---|
|[per_request_4pct](per_request_4pct/README.md)|每条请求约4%未命中，命中前缀向下对齐16-token块|207547|4.055431%|539.258683|旧标签A90/B270|
|[global4_abc](global4_abc/README.md)|整批input的4%作为总计算额度，分配A/B/C|204699|3.999781%|539.571462|A240/B90/C30|
|[global4_abcd](global4_abcd/README.md)|在相同全局额度内增加D类|204699|3.999781%|539.571462|A220/B90/C30/D20|

总input均为5117755 token。简单版本的4%是逐请求规则。
ABC、ABCD的4%是全局额度，两种口径不能混用。
全局理论额度为204710.2；block16下不超过额度的最近可行值为204699。

## 类别含义

ABC与ABCD使用下列人工读算成本分类：

- A：重读、轻计算。只计算块对齐所需的1–16个token。
- B：轻读、重计算。承接全局剩余计算额度。
- C：零读、计算。最短30条请求全部作为新计算。
- D：重读、重计算。最长20条请求各计算2033–2046个token，其余读取。

ABCD从B转出40640个新计算token分给D，保持总计算额度和总读取量不变。
简单版本的A90/B270是历史展示标签，不能按上述A/B含义解读。
这些标签都不替代运行期TFS的P75分类。

## 文件与配置

每份负载包含完整输入的`trace.jsonl.gz`、可读的`requests.csv`、
统一比较配置`config.json`、原实验参数`config.historical.json`和来源指纹`manifest.json`。
JSONL只压缩，不改内容；解压后与原实验输入逐字节一致。
不复制74 MB未压缩token数组、原始事件日志或运行环境。

统一比较配置使用90 GB/s单共享池、K=1、12实例、block16、每步最多4请求/8192新token，
GLM5实际组批v2、TP1/DP8/EP8、78层流水，实例内EDF。
默认配置为直接准入；TFS与α由实验运行器选择，避免把某个算法固定进负载定义。
比较配置保留同步batch反馈、关闭coalesced反馈的实验标记。
这些标记需要回放驱动实现；导出工具不会自动安装实验运行时包装。

简单版本的历史配置是120 GB/s，ABC、ABCD的历史配置是90 GB/s。
本分支交付时对简单版本的90 GB/s直接准入做了完整流程验收；三臂性能对照尚未补跑。
配置变化后必须重算请求画像、T0及期限，不直接复用旧SLO结果。
`config.historical.json`保留原实验参数，移除本机overlay路径；原文件SHA仍记录在manifest中。
`config.json`移除了本机Dynamo overlay路径。

## 校验与导出

在工作区根目录执行。工具只需要Python标准库，不需要导入DV或SA。

```bash
python3 benchmark/manage.py verify
python3 benchmark/manage.py export global4_abc --output temp/benchmark_abc_input
```

导出目录必须不存在。导出得到DV所需的`trace.jsonl`与`config.json`。
另两份可将名称换成`per_request_4pct`或`global4_abcd`。
需要加权Dynamo构建时，用`--dynamo-python <本机overlay目录>`明确指定路径。
导出只准备输入，不启动仿真，也不自动选择算法、预热或资源保护参数。

`manage.py verify`检查四份共1440条请求的长度、h/u、块对齐、token序列、
请求顺序、到达时刻、类别、总量、配置与SHA256。
[catalog.json](catalog.json)是机器可读目录；[validation.json](validation.json)保存本次校验结果。
本分支直接提供冻结输入。日常复用不依赖原工作区、SA仓库或历史results/temp目录。

AgentX另检查源行号、历史命中映射、12秒缩放、180GB/s、39条长上下文和三方案合同。配额为动态P75计算，不是固定5条；本case不代表平均负载低于100%。
