# 四实例 TFS 策略入口

使用 `bash scripts/run_4w25.sh 负载 策略 新输出目录`。以下策略共用四个 worker、每实例批大小 1、MiniMax 16 层和六盘共享 25 GB/s 的 CPU 仿真环境。

## 选择策略

| 参数 | 启用的策略 |
| --- | --- |
| `tfs` | 普通 TFS，P75 判重和整数额度。 |
| `bandwidth` | 带宽加权准入。重读需求须超过 6.25 GB/s 且达到 P75。central 按需求记账。账本为空时允许一条超过总容量的重读独占许可。 |
| `snapshot-light` | 强制全轻读，开启全量开工上报和本地带宽快照选队。 |
| `snapshot-tfs` | 带宽加权准入加全量开工上报与本地带宽快照选队。它是重读分散对照的 TFS 基线。 |
| `spread5`、`spread10` | 在 `snapshot-tfs` 基础上，重读从 KV 命中相同且原评分在最低分 5% 或 10% 以内的候选中，优先选等待重读少的 worker。 |
| `spread100` | 在 `snapshot-tfs` 基础上，取消候选分差上限。在 KV 命中相同的可用候选中优先减少等待重读条数。 |

开工上报覆盖普通请求，包括低于 1 GB/s 和零外部读的请求。重读分散在 Dynamo 首次路由时读取现有账本中的等待条数。原 α=0.1 路由继续使用同一账本中的等待 token 数。

## 构建和启动

在 Linux 或 WSL 进入仓库根目录。按 [CPU 环境与 Dynamo 构建说明](服务器三实例仿真.md#构建与启动) 准备 vLLM、Rust 1.96.1 与 NIXL。启用 `spread*` 前，用本分支的 `source_dev/dynamo` 重新构建一次。四个策略档可以共用一个构建。

```bash
export DV_PYTHON="$PWD/dynamo_vllm_cpu_sim/.venv/bin/python"
export RUSTUP_TOOLCHAIN=1.96.1
BUILD="$HOME/.cache/dynamo-server-sim/build-heavy-spread"
bash dynamo_vllm_cpu_sim/scripts/build_server_dynamo.sh "$PWD/source_dev/dynamo" "$BUILD" "$DV_PYTHON"
"$DV_PYTHON" -m pip install --target "$BUILD/python" --no-deps nixl==0.10.1 nixl-cu12==0.10.1
export DYNAMO_PYTHON_DIR="$BUILD/python"
RESULTS=$(mktemp -d "$HOME/four-worker-tfs.XXXXXX")
```

```bash
# 单独运行加权准入或带宽上报
bash scripts/run_4w25.sh synthetic770 bandwidth "$RESULTS/770-bandwidth"
bash scripts/run_4w25.sh synthetic770 snapshot-tfs "$RESULTS/770-snapshot-tfs"

# 重跑两条负载的重读分散对照
for CASE in synthetic770 agentx-mooncake540; do
  for ARM in snapshot-tfs spread5 spread10 spread100; do
    bash scripts/run_4w25.sh "$CASE" "$ARM" "$RESULTS/$CASE-$ARM"
  done
done
```

各轮共用本机监听端口，依次运行。输出目录须是新路径。770 使用 35 秒冻结到达。混合 540 使用 50 秒冻结到达，含 AgentX 360 条和 Mooncake Chat 180 条。其他负载名见 [四实例运行指南](四实例25GB运行指南.md)。通过 `benchmark/manage.py` 可独立校验或导出冻结 trace。

## 输出与报告

- `summary.json` 汇总成功请求数、首 token 延迟、整批以及四实例各自完成时间。整批和实例完成时间以最后一个完整响应计。
- `run/routing.launch.json` 记录 frontend 启动参数。`run/frontend.log` 中的 `TFS_HEAVY_SPREAD` 记录重读候选原分、等待条数和最终落点。
- `run/admission.events.jsonl` 记录重读许可与带宽上报。`run/check.json`、`run/layer_check.json` 和 `run/read_admission.check.json` 保存验收结果。

两份报告均为每档一轮的 CPU 仿真。[Synthetic770 报告](../report/mooncake_synthetictail_770_35s_4_25GBps/README.md) · [混合 540 报告](../report/agentxmooncake_coldchat_540_50s_4_25GBps/README.md)
