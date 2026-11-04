# 重绘与重跑

本目录保存已验收的新运行报告。标准dataset、配置、逐请求表、真实batch表、准入表、逐盘带宽表和来源SHA已公开；大规模原始层/RPC事件、依赖和原生扩展不公开。

从仓库根目录执行：

```bash
python scripts/manage_reports.py redraw "mooncake_abcd_360_4s_12_100GBps/20261102-214828_多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1" --output <新的输出目录>
```

该命令只解压并读取冻结标准dataset，不再运行性能实验。公开dataset省去重复的逐事件盘数组，总速率事件与逐盘10ms积分表完整保留；与源CSV的汇总指标一致，见dataset_compaction.json。随附reproduce目录保留本次固定绘图工具，手动使用时需先解压dataset.json.gz。新图需重新检查，不能沿用原图的视觉验收。

完整性能重跑需准备主README中的冻结运行环境，再使用独立多盘五方案矩阵说明。原benchmark默认仍是共享池，必须显式指定本报告拓扑。图8来自记录提交时刻的冻结存储模型重放，逐次完成和每盘字节积分已核对；仅有公开dataset不能重新审计未发布的原始事件。

source_validation.json验证源生成目录；archive_validation.json验证公开压缩归档。manifest中的simulations_run=0专指归档阶段；source_execution记录本任务实际执行的五次正式运行。
