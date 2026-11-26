# Mooncake Synthetic 720 · 62.5% · 35 秒

取原始 Synthetic 第 2496–3215 条请求。前 2495 条提供暖缓存历史。沿用 40 秒冻结输入的 token 与命中前缀，到达时间乘 0.875。远端前缀按 128 token 对齐，命中率 85.90%。每条生成 1 个 token。五条超长输入截到 97,792 token。

四实例，每实例最多一个序列。MiniMax 16 层计算模型。六盘共享 25 GB/s，各盘约 4.167 GB/s。默认直发与普通 TFS 的生效参数分别在 config.default.json 和 config.tfs.json。

统一 benchmark 工具支持校验与无损导出。本报告的默认直发使用 Mac 研究版 immediate 入口。交付分支旧统一运行器和 run_4w25.sh 的 light 策略有不同的运行合同。完整复跑需准备与报告所列对应的研究运行环境。

[实验报告](../../report/mooncake_syntheticp625_720_35s_4_25GBps/20261126-223923_默认直发+普通TFS/report.md)
