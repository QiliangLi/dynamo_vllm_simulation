# 复现

从仓库根目录运行：

```bash
python scripts/manage_reports.py redraw "agentx_flex_358_12s_4_25GBps/20261126-090103_默认_TFS_带宽账本" --output <新目录>
```

完整重跑使用 `bash scripts/run_4w25.sh agentx-flex light|tfs|bandwidth NEW_OUTPUT_DIR`，每个策略分别启动。原始逐层事件保留本机；标准派生数据支持离线重绘。
