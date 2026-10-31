# 复现范围

- 可复核：report_data 内四臂汇总表、逐请求 TTFT（gzip CSV）与 lease 账本审计摘要；manifest 记录文件 SHA。
- 完整重跑需 WSL 仿真环境与加权 Dynamo 构建（见仓库 docs/实验分支使用说明.md 与 scripts/run_benchmark.py 入口）；本报告不附大型原始事件。
- 调参轮（tune-*）与源运行目录保存在运行机 results/benchmark_runs/。
