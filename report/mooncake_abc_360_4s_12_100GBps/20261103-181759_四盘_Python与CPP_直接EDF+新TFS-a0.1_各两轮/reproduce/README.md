# 重绘

从仓库根目录执行：

```bash
python scripts/manage_reports.py redraw "mooncake_abc_360_4s_12_100GBps/20261103-181759_四盘_Python与CPP_直接EDF+新TFS-a0.1_各两轮" --output <新的输出目录>
```

只读取已冻结的标准数据，不运行仿真。源报告的七张PNG保持原样。大规模原始层事件不发布；公开数据可重绘和复核汇总，不能重新审计缺失的原始事件。source_validation.json是源报告验收，archive_validation.json是归档完整性验收。

仓库当前绘图器还会生成第八张逐盘带宽附图。归档仍保留原来的七张图；重绘的这七张已通过逐字节核对，见 ../report_data/redraw_check.json。
