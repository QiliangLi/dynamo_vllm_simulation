# 统一 benchmark 与独立启动开关

同事首次运行服务器 `synthetic_abc` 仿真，请按[服务器负载与仿真运行指南](../docs/服务器SyntheticABC与仿真运行指南.md)执行。包含安装、原始到达回放及 α=1/dev 对照。

benchmark 只构建负载、准备缓存请求、发送 HTTP 请求并保存客户端结果。不读取进程、TFS 配置或 central 日志，不启动、停止、重启服务，不选择策略。

prepare 和 replay 中的单条 HTTP、流式响应或超时失败只记录，不导致命令以失败状态退出，不自动重试。prepare 全部请求结束后，replay 跳过准备失败的对应请求，继续发送准备成功的请求和不需要准备的 C 类请求。保留原始到达时刻，不补发、不压缩时间轴。

报告保留原计划数量、本阶段计划发送数量、失败数和跳过数。跳过明细写入 `replay/skipped_requests.jsonl`，不计为成功；每类跳过数保存在 `summary.json`。全部跳过时输出 `no_requests_sent` 和空统计。TTFT 仅统计成功且有可见文本的请求。策略对比须同时报告失败及跳过数量，因为有效负载可能不同。结果缺失、准备记录不完整、配置错误、文件错误和主动中断仍报错退出。

这里的准备成功仅指客户端收到正常完成响应。OD 写入失败但服务仍返回成功时，benchmark 无法据此自动跳过；实际缓存持久化和命中仍需服务端证据。

启动脚本通过 `--tfs on|off` 设置服务模式，默认 on。开关只对新启动的进程生效。安装工具和 benchmark 是两个独立入口；benchmark 不调用安装工具。

## 安装启动参数

在服务器容器的 drt_dev 目录执行：

```bash
unzip -n benchmark_tools_20261116.zip
python3 benchmark_tools_20261116/install_tfs_switch.py --check
python3 benchmark_tools_20261116/install_tfs_switch.py --apply
```

安装工具读取服务器当前六份脚本，检查 native-ingress 标记和启动位置，通过 `bash -n` 后备份，再增量插入参数解析及最终环境覆盖块。原始 setsid 启动命令体保持不变。不用本地旧启动脚本覆盖服务器脚本。

备份位于 `temp/tfs_switch_...`，权限 0700，可能含原脚本中的凭据。不要公开上传备份。回滚命令：

```bash
python3 benchmark_tools_20261116/install_tfs_switch.py --rollback /实际BACKUP目录
```

回滚前检查当前文件和备份 SHA256；若安装后又有修改则拒绝覆盖。

`--tfs off` 清除 TFS 准入、画像、等待评分的环境变量；worker 移除 `tfs12` 插件，保留其他已选插件。`--tfs on` 使用脚本现有 TFS 配置。OD connector、事件发布、layerwise、模型参数、卡号、端口、构建路径、namespace、worker index 均保持原脚本设置，不自动重新编号或更改 central 配额。

`5.run_tfs_central.bash --tfs off` 只跳过启动，不会停止已经运行的 central。

## 手动启动

用户先停止上一组服务，避免不同模式的 worker 共用同一个路由池。切换策略时重新启动 frontend 和 worker；启用 TFS 时重新启动 central，避免复用旧评分服务身份。

默认组：

```bash
bash 4.run_dynamo.bash --tfs off
bash 1.run_vllm_single_minimax_m2.5.bash --tfs off
bash 2.run_vllm_single_minimax_m2.5.bash --tfs off
bash 3.run_vllm_single_minimax_m2.5.bash --tfs off
```

TFS 组，先等 central 就绪，再启动 frontend 和 worker：

```bash
bash 5.run_tfs_central.bash --tfs on
bash 4.run_dynamo.bash --tfs on
bash 1.run_vllm_single_minimax_m2.5.bash --tfs on
bash 2.run_vllm_single_minimax_m2.5.bash --tfs on
bash 3.run_vllm_single_minimax_m2.5.bash --tfs on
```

两组必须自行确认所有目标 worker 注册完成、OD 正常，再发送 benchmark。benchmark 不做这项服务检查。

## 统一发送命令

### prepare 写入 KV，replay 关闭新增 KV 写入

在 `run` 或 `send` 命令中加入 `--replay-phase2-dump off`。prepare 的 HTTP 请求显式携带
`"kv_transfer_params": {"enable_phase2_dump": true}`，replay 的全部类别（含纯 miss 的 C 类）携带
`"kv_transfer_params": {"enable_phase2_dump": false}`。这是 JSON 布尔值，不是字符串。

`--replay-phase2-dump on` 显式启用两个阶段的写入。不传此参数时，两阶段都不添加这个字段，沿用服务端默认值。
该选项只控制请求字段，不选择 TFS 策略，不启停服务，也不改变 trace 的一次回放限制。
两个对照组必须使用相同选项。实际请求配置保存在 `prepare_config.json`、`replay_config.json` 和各阶段的 `run.json`。

使用前，Dynamo frontend 与 worker 必须加载参数透传版本，OD connector 必须加载请求级开关补丁。
HTTP 请求成功只证明请求完成；replay 无 PUT、无新增 DISK BlockStored、GET 保持正常仍需结合服务端日志核验。

本地 TFS 示例配置使用 6 块盘，共享 DPU 口 25,000,000,000 B/s，单盘按总带宽除以 6 建模。
这个单盘值是均分预算，不是独立实测吞吐。`synthetic_abc_3w` 的设计元数据同步为 25 GB/s；请求长度与到达时刻不变。

```bash
python3 -u benchmark_tools_20261116/benchmark.py run \
  --benchmark synthetic_abc \
  --endpoint http://127.0.0.1:8000/v1/completions \
  --prepare-concurrency 3 --concurrency 360 \
  --replay-phase2-dump off --label default_no_dump
```

TFS 服务启动后使用同一命令，把记录标签改成 `tfs_no_dump`。

### 原有发送方式

```bash
python3 -u benchmark_tools_20261116/benchmark.py run \
  --benchmark mooncake_abc \
  --endpoint http://127.0.0.1:8000/v1/completions \
  --prepare-concurrency 3 --concurrency 360 \
  --label default
```

TFS 组使用同一命令，可将 label 改为 tfs。label 只写入记录；任意名称都合法，不影响请求或服务。每次生成新前缀，避免直接复用上一组文本；相同 benchmark 的 H/U 和到达时刻一致。`workload_id` 用于确认负载结构一致。

可选 benchmark：pilot24、mooncake_abc、mooncake_abcd、synthetic_abc、agentx。默认 paced 回放，time-scale=1；可显式选择 `--mode serial|paced|burst|concurrent`。`--time-scale 2` 将到达间隔扩大两倍。prepare 并发与 replay 并发独立。

mooncake_abc 每组准备 330 条前缀请求，等待默认 10 秒，然后按约 18 秒窗口回放 360 条请求。总运行时间还包含数据生成、缓存准备和排队完成时间。等待 10 秒不证明缓存已经持久化。每阶段每 30 秒报告进度；逐请求输出在 `prepare.log` 和 `replay.log`。

只生成、不发送：

```bash
python3 benchmark_tools_20261116/benchmark.py build --benchmark mooncake_abc --output /新目录/data
```

发送已构建的 trace，仍会自动执行 prepare/replay：

```bash
python3 -u benchmark_tools_20261116/benchmark.py send --trace /新目录/data/trace \
  --endpoint http://127.0.0.1:8000/v1/completions --prepare-concurrency 3 --label runA
```

一份 trace 只允许正式回放一次。重复比较应重新 build/run，不能删除回放标记。每次结果打印 OUTPUT 和 SUMMARY。比较结果只读取文件：

```bash
python3 benchmark_tools_20261116/benchmark.py summary /第一组OUTPUT /第二组OUTPUT
```

本轮只比较客户端延迟与完成率，无 SLO 目标。没有可见文本的正常结束记成功但 TTFT 为空。H/U 是目标输入和预期缓存命中；实际 OD 命中、传输、许可释放需要单独核对。工具不清理共享存储，不消除运行顺序、缓存容量和外部负载的影响。

## 本地验证

8 项 CPU 测试通过：六个启动脚本的 Bash 语法与启动体保留、默认/显式开启、关闭环境且保留 OD 配置、非法参数拒绝、central 关闭时跳过启动、备份与带哈希保护的回滚、负载标签不影响请求生成、模拟 HTTP prepare/replay 和单次回放保护。真实服务器尚需安装并验证。

本包替代了 [早期 Mooncake 对照工具](../temp/mooncake_compare_20261116/README.md) 的服务与 benchmark 联动用法。
