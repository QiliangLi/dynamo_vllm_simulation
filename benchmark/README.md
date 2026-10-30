# Benchmark

本目录保存可追溯的固定请求负载及其运行配置。每个 benchmark 使用一个文件夹；具体名单、构造说明、支持状态和字段解释统一见 [benchmark目录.md](benchmark目录.md)。AI 使用方法见 [agent.md](agent.md)。

## 命名

```text
数据来源_数据特征_请求总数_发送窗口_计算实例数_存储带宽
```

来源与特征使用小写字母或数字；发送窗口带 s，存储带宽带 GBps。GBps 表示十进制 GB/s。实例数指逻辑计算实例数；存储带宽指共享池总容量。名称必须与 manifest.json、config.json 和实际请求一致。

random 表示未按 ABC/ABCD 刻意分配请求类别，不表示随机打乱或没有其他数据处理。特征定义放在总目录附录，具体来源与处理规则放在负载自己的说明和 manifest 中。

## 文件夹内容

|文件|作用|
|---|---|
|README.md|这份负载的来源、处理方法和特殊限制|
|trace.jsonl.gz|冻结的完整请求数据，gzip 只压缩、不改变 JSONL 内容|
|requests.csv|逐请求可读明细，含长度、命中、新算、到达和读取量|
|config.json|这份 benchmark 的运行配置，含模型和资源条件|
|config.historical.json|来源实验的历史配置，仅用于追溯|
|manifest.json|身份、来源版本、SHA256、统计总量、支持状态等机器可读信息|
|run_contract.json（可选）|特殊反馈、预热、外推边界和方案配置等合同|

计算模型、反馈、block 和 SLO 等未写进名称的信息，必须在配置和合同中保留。不得根据名称补猜默认参数。重新运行时按当前有效配置计算画像和期限。

## 查找、校验和导出

从仓库根目录运行；这些工具只需 Python 标准库，不启动模拟器：

```bash
python benchmark/manage.py list
python benchmark/manage.py verify
python benchmark/manage.py catalog --check
python benchmark/manage.py export <benchmark名称> --output <新的输出目录>
```

导出目录必须不存在。导出提供 trace.jsonl、config.json、manifest.json，以及存在时的 run_contract.json。本机加权 Dynamo 路径通过可选参数 --dynamo-python 显式传入，不写进冻结配置。

完整运行方式见仓库 [实验分支使用说明](../docs/实验分支使用说明.md)。只有支持状态为 supported 的负载可以交给当前运行器；export_only 只支持校验和导出。

## 新增与维护

新增一个符合规则的文件夹，完成数据与配置校验，再运行：

```bash
python benchmark/manage.py verify
python benchmark/manage.py catalog
python benchmark/manage.py catalog --check
```

工具从文件夹 manifest 自动发现负载，不维护第二份固定名单。普通新增只增加负载文件夹并更新总目录；总 README 和 agent.md 仅在格式或使用流程变化时修改。

SHA 校验保证文件一致，不证明模型准确、运行合同已经实现或性能结论成立。运行记录、图表和大型原始事件放到独立结果目录。
