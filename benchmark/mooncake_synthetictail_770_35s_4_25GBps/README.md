# Mooncake 720 + Synthetic 尾部 50

四个 CPU 仿真实例采用 FCFS，每步最多处理一条请求。六盘共享 25 GB/s。使用 MiniMax 16 层、TP=2 等效计算模型 `prefill50k_total16_v2`，每条请求生成一个 token。

- 主负载为 [已发布的 Mooncake Synthetic 720](../mooncake_syntheticp625_720_35s_4_25GBps/README.md)，取原始第 2496–3215 行。720 条保持原来的 token、命中长度和 0–35 秒到达时刻。
- 追加原始第 1–50 行。计划到达时间为 `30 + 原始时间戳秒数 × (35 / 178.369)`，范围为 30.000–32.848 秒。其中 49 条不读 KV，1 条读取 1.913 GB。
- 合计 770 条请求，读取 713.007 GB。`requests.csv` 的 `cohort` 标明请求来源，`head50_requests.json` 保存追加请求的原始时间戳和读取量。

## 校验和导出

在仓库根目录执行以下命令。输出目录必须是新目录。

```bash
CASE=mooncake_synthetictail_770_35s_4_25GBps
OUT=./exported-mooncake770
python benchmark/manage.py verify
python benchmark/manage.py export "$CASE" --output "$OUT"
```

导出的 `trace.jsonl` 与实验输入逐字节一致。旧统一运行器支持校验和导出。原实验用四实例 Dynamo 1.2 + CPU vLLM server-stack。配套环境见[四实例运行指南](../../docs/四实例25GB运行指南.md)。在已有兼容 harness 上，使用同一份导出的 trace 分别运行下面两组。

| 策略 | `run_tfs_stack.py` 参数 | 配置 |
|---|---|---|
| 默认直发 | `--workers 4 --mode immediate --no-idle-bypass` | `benchmark/$CASE/config.default.json` |
| 普通 TFS | `--workers 4 --mode tfs --no-idle-bypass` | `benchmark/$CASE/config.tfs.json` |

`config.json` 采用本次普通 TFS 的冻结配置。两组使用相同的计算和存储设置。普通 TFS 启用原生入口、Dynamo 分层画像、调度器准入及路由记账。详见 [实验配置清单](experiment.json)。

## 四实例重读分散回放

用 `bash scripts/run_4w25.sh synthetic770 spread10 NEW_OUTPUT_DIR` 运行。四档对照与构建步骤见[四实例 TFS 策略入口](../../docs/四实例TFS策略入口.md)。
