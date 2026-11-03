# 重绘与重跑

从仓库根目录执行：

```bash
python scripts/manage_reports.py redraw "synthetic_abc_360_6s_12_90GBps/20261103-194402_CPP多盘_直接FCFS+EDF_旧TFS-EDF_新TFS-EDF-a0.1+a1" --output <新的输出目录>
```

只读冻结标准数据并生成八张图，不运行性能实验。原始大事件不公开；公开数据足以重绘和核对汇总，不能重审未发布的原始逐层事件。完整性能重跑按矩阵说明，显式使用 --storage-backend cpp。

source_validation.json验证源报告；archive_validation.json验证压缩归档。simulations_run=0只指制图归档阶段，source_execution记录本轮实际五次正式运行。
