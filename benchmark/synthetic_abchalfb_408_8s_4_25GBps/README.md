# synthetic_abchalfb_408_8s_4_25GBps

A120: H20480/U2048；B144: H4608或4736/U2048；C144: H0/U384。8秒到达，逐层提交。

4实例，TP2等效，16层MiniMax prefill50k_total16_v2，每步最多1条；6盘共享25 GB/s。请求总量 408，读取 205.151797248 GB。

保留本次实验的完整token、请求ID、H/U、顺序及到达时间；不重新生成。窗口为0到标称结束时间，实际首尾计划到达为 0.0078431372549 / 7.999 秒。

## 使用

从仓库根目录执行：

```bash
python benchmark/manage.py verify
python benchmark/manage.py export synthetic_abchalfb_408_8s_4_25GBps --output /tmp/synthetic_abchalfb_408_8s_4_25GBps
```

导出目录须使用新路径。要运行这份冻结 trace，请使用[四实例 25 GB/s 运行指南](../../docs/四实例25GB运行指南.md)中的专用入口。旧 GLM5 入口保留 export_only 标记。

对应报告见[报告目录](../../report/synthetic_abchalfb_408_8s_4_25GBps/README.md)。来源及合同见[experiment.json](experiment.json)。
