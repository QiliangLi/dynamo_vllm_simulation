# synthetic_abclonghit_540_12s_4_25GBps

A60: H51200/U768；B240: H2048/U4096；C240: H0/U384。12秒到达。

4实例，TP2等效，16层MiniMax prefill50k_total16_v2，每步最多1条；6盘共享25 GB/s。请求总量 540，读取 233.538846720 GB。

保留本次实验的完整token、请求ID、H/U、顺序及到达时间；不重新生成。窗口为0到标称结束时间，实际首尾计划到达为 0.0100667514545 / 12 秒。

## 使用

从仓库根目录执行：

```bash
python benchmark/manage.py verify
python benchmark/manage.py export synthetic_abclonghit_540_12s_4_25GBps --output /tmp/synthetic_abclonghit_540_12s_4_25GBps
```

导出目录须使用新路径。要运行这份冻结 trace，请使用[四实例 25 GB/s 运行指南](../../docs/四实例25GB运行指南.md)中的专用入口。旧 GLM5 入口保留 export_only 标记。

对应报告见[报告目录](../../report/synthetic_abclonghit_540_12s_4_25GBps/README.md)。来源及合同见[experiment.json](experiment.json)。
