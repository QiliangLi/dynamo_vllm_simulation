# agentxmooncake_coldchat_540_50s_4_25GBps

AgentX 360 条与 Mooncake Chat 180 条，共 540 条。两路分别从空缓存开始，按更早到达请求的完整前缀块判定命中。同刻到达的一批先判定，再更新两路各自的历史。

保留同一批 36 秒输入的 token、请求 ID、命中量和回放顺序，将到达时刻乘以 `50/36`。两路的计划发送区间均为 0–50 秒。总 KV 读取量为 839.515111424 GB。

四实例 MiniMax CPU 仿真，16 层，每实例批大小 1。六盘共享 25 GB/s。AgentX 使用源行 72863–73222，Mooncake Chat 使用原始前 180 条。冻结 token ID 用于控制仿真读量。

## 校验和导出

在仓库根目录执行以下命令。

```bash
python benchmark/manage.py verify
python benchmark/manage.py export agentxmooncake_coldchat_540_50s_4_25GBps --output /tmp/agentxmooncake_coldchat_540_50s_4_25GBps
```

此 benchmark 的发布状态为 `export_only`。完整四策略重跑使用对应的研究仿真环境与策略覆盖。默认全轻读需要仿真启动时启用全轻读覆盖。四策略配置文件见 [experiment.json](experiment.json)。

[四策略报告](../../report/agentxmooncake_coldchat_540_50s_4_25GBps/README.md)
