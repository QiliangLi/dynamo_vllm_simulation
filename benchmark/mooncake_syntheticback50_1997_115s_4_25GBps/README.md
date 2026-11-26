# Mooncake Synthetic 后半段 1,997 条 · 115 秒

本用例保留原始轨迹的第 1997–3993 条请求。前 1,996 条用于推导已有缓存。到达时间沿用上一轮的顺序和间隔比例，按 115/120 缩放。远端 KV 前缀按 128 token 对齐，命中率 86.19%。九条超长输入截到 97,792 token。每条生成 1 个 token。

仿真使用四个实例，每实例最多一个序列。计算采用 MiniMax 16 层模型。六盘共享 25 GB/s。默认直发与普通 TFS 各运行一次。两组生效参数见 `config.default.json`、`config.tfs.json` 和 [实验报告](../../report/mooncake_syntheticback50_1997_115s_4_25GBps/20261127-002205_默认直发+普通TFS/report.md)。

## 校验与导出

在仓库根目录执行：

```bash
python benchmark/manage.py verify
python benchmark/manage.py export mooncake_syntheticback50_1997_115s_4_25GBps --output /tmp/mooncake-1997-115s-input
```

统一工具支持固定输入校验和无损导出。本用例使用 Mac 研究版的 `immediate` 和 `tfs` 两种入口。四实例交付脚本中的 `light` 使用另一个运行合同。

## 使用对应的研究环境复跑

准备 [版本及文件哈希](runtime_versions.json) 所列的研究版模拟器，以及原生 Dynamo 1.2 的构建目录。按当前机器设置路径变量。`DELIVERY_REPO` 指向交付仓库。`DEFAULT_SIM_ROOT` 和 `TFS_SIM_ROOT` 分别指向两组实验对应的模拟器。`SIM_PYTHON` 指向模拟器的 Python 解释器，`DYNAMO_PYTHON_DIR` 指向 Dynamo 构建的 `python/` 目录。`RUN_OUTPUT` 指向新输出目录。复用上一节导出的 `trace.jsonl`。

```bash
case_dir="$DELIVERY_REPO/benchmark/mooncake_syntheticback50_1997_115s_4_25GBps"
trace=/tmp/mooncake-1997-115s-input/trace.jsonl
export TMPDIR=/tmp PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS=""
export DYN_TCP_RPC_HOST=127.0.0.1 DYN_TCP_REQUEST_TIMEOUT=60
export DYN_TFS_CONTROL_TIMEOUT_MS=3000
cd "$DEFAULT_SIM_ROOT"
"$SIM_PYTHON" scripts/run_tfs_stack.py --config "$case_dir/config.default.json" --trace "$trace" \
  --output "$RUN_OUTPUT/default" --workers 4 --mode immediate --no-idle-bypass --server-dynamo "$DYNAMO_PYTHON_DIR"
cd "$TFS_SIM_ROOT"
"$SIM_PYTHON" scripts/run_tfs_stack.py --config "$case_dir/config.tfs.json" --trace "$trace" \
  --output "$RUN_OUTPUT/tfs" --workers 4 --mode tfs --no-idle-bypass --server-dynamo "$DYNAMO_PYTHON_DIR"
```

两组依次执行。原运行的 HTTP 客户端使用开放式发压，最大连接数为请求数，响应超时为 480 秒。TFS 的运行后校验从完整事件日志补全准入快照。固定输入和报告可以独立使用，精确复跑依赖上述研究环境。
