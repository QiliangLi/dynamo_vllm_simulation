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

输出目录必须不存在。导出包含完整trace、配置和experiment.json。此负载标记为export_only：本次发布固定输入和已有报告，不把研究版server-stack运行合同冒充旧GLM5运行器支持。config.json是原运行配置，默认全轻读还需要研究harness的force-light覆盖；不能只看admission.mode判断运行策略。报告是仿真，不是真机数据。

完整运行涉及原生Dynamo 1.2、CPU vLLM、当前计算模型及策略覆盖；通用环境说明见[服务器运行指南](../../docs/服务器SyntheticABC与仿真运行指南.md)，该指南的三实例synthetic_abc命令不等于本固定trace的重跑命令。不要直接把仿真token输入当作真机prepare/replay种子。

对应报告见[报告目录](../../report/synthetic_abclonghit_540_12s_4_25GBps/README.md)。来源及合同见[experiment.json](experiment.json)。
