# Dynamo + vLLM / Ascend 的 CPU 调度仿真启动包

本工程已经跑通真实 Dynamo SelectionService、vLLM 0.20.2 Scheduler / KVCacheManager，以及按文件加载的 Ascend 0.20.2rc1 BalanceScheduler。NPU 计算和远端存储由事件驱动模型代替。**这是可运行的调度核心实验台，不是完整 Dynamo 服务 + vLLM EngineCore + 全量 Ascend 插件的 CPU 移植。**

详细设计见 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)，实测和适用范围见 [VALIDATION.md](VALIDATION.md)。

## 直接启动

已验证平台：Linux x86_64、Python 3.12、CPU。建议使用独立机器/容器或 WSL2；macOS、Windows 原生和 ARM 未验证。需要能够下载 GitHub 源码、PyPI 包及 CPU PyTorch；无需模型权重、CUDA、CANN、NPU、etcd 或 NATS。初次准备建议预留数 GB 磁盘空间。

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后：

```bash
unzip dynamo_vllm_cpu_sim.zip
cd dynamo_vllm_cpu_sim
bash scripts/bootstrap.sh
```

脚本会按 SHA 下载三个上游仓库、建立 `.venv`、安装固定依赖、以 `VLLM_TARGET_DEVICE=empty` 安装真实 vLLM Python 源码、检查来源并跑完 12 个请求。结果在 `results/bootstrap/report.json` 和 `events.jsonl`。脚本会拒绝覆盖来源不明的已有源码目录。

若环境使用代理，按当地网络要求设置 `HTTPS_PROXY` / `HTTP_PROXY` 后运行；项目不会自行修改系统网络设置。启动后不需要联网获取模型。

## 可复跑的实验

所有命令均从工程根目录运行：

```bash
export PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1

# 默认：真实 Ascend BalanceScheduler 类，balance 开关关闭，走其原始委托路径
.venv/bin/python -m sim.run --output results/ascend_default

# 直接使用真实 vLLM Scheduler
.venv/bin/python -m sim.run --scheduler upstream --output results/upstream

# 真实 vLLM priority 策略；缺省 priority 为 prompt 长度减声明的远端前缀长度
.venv/bin/python -m sim.run --policy priority --output results/priority

# 慢存储对照：共享链路由 10 GB/s 改为 0.1 GB/s
.venv/bin/python -m sim.run --config configs/slow_storage.json --output results/slow

# 可选：生成显式补丁副本，再开启 Ascend balance 调度
.venv/bin/python scripts/apply_ascend_patch.py
.venv/bin/python -m sim.run --scheduler ascend_balance --output results/ascend_balance

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_results.py results/ascend_default results/upstream results/slow
```

`ascend_balance` 补丁只针对本工程复现的异步 KV 状态更新问题，未在 NPU 验证。脚本不改上游文件。该模式为每个 worker 一个 DP rank，不模拟真实 DP collectives。

## 代码入口

| 文件 | 职责 |
|---|---|
| `sim/router.py` | 调用真实 Rust SelectionService；reserve / prefill complete / free 的记账同步 |
| `sim/engine.py` | 构造真实 Scheduler、Request、KVCacheConfig；生成仿真 ModelRunnerOutput |
| `sim/connector.py` | 真实 KVConnectorBase_V1 接口：命中、分配、异步完成 |
| `sim/storage.py` | 共享 KV 池、磁盘与路径排队、动态链路带宽 |
| `sim/run.py` | 全局虚拟时间、事件推进、指标和 JSON 记录 |
| `upstream/vllm/vllm/v1/core/sched/scheduler.py` | 你要修改的真实 vLLM 调度器 |
| `upstream/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py` | Ascend 调度适配源码 |
| `upstream/dynamo/lib/kv-router/src/scheduling/selector/policy.rs` | Dynamo 的真实 Rust 策略接口；修改后必须重编译 Python 扩展 |

依赖分两种：`requirements.in` 是人工整理的导入依赖入口；`requirements.lock` 是验证环境锁定的完整安装集合。启动脚本使用 lock 和 `--no-deps`，这是裁剪设备依赖的 CPU 实验环境，不能作为 NPU 生产安装方式。`pip check` 可能报告未安装的设备相关生产依赖；本工程用实际导入和集成运行验证选定代码路径。

## 输入与输出

每行一个 JSON，请求时间单位为秒、吞吐单位为 bytes/s：

```json
{"id":"request-1","arrival_s":0.0,"prompt_token_ids":[11,12,13,14],"output_tokens":8,"remote_prefix_tokens":0,"priority":0}
```

`remote_prefix_tokens` 必须为 block_size 的整数倍，声明仿真开始前共享存储中已存在的前缀；同一个前缀对所有请求可见。不要把这字段用于声明一个未来才生成的缓存。真正的命中仍由 vLLM 的内容哈希和连续前缀规则决定。priority 数值越小优先级越高。

`configs/demo.jsonl` 是合成冒烟负载。例子中的带宽、KV 字节数及计算系数都不是任何 NPU 型号的实测值。

输出包括首 token / 完成时间、读字节数、每 worker 的 compute / stall / idle 时间、真实 scheduler 文件哈希、结束后的块回收及 Dynamo 记账状态。`events.jsonl` 可检查 `WAITING_FOR_REMOTE_KVS → kv_received → token` 的因果顺序。

## 适用边界

当前支持：聚合式单模型服务、多个逻辑 worker、每 worker 单 rank、同步 scheduler、chunked prefill、逻辑前缀缓存、只读远端 KV 池、有限正输出长度、合成确定 token。这里的同步是 scheduler 模式；远端存储仍异步完成。

尚未实现：Dynamo KV 事件同步/真实缓存感知路由、完整 EngineCore/服务前端、完整 Ascend 插件导入、逐层 I/O 与计算重叠、PD 分离、TP/PP/HCCL、KV 写回、故障与取消、MoE/MLA/混合注意力模型、数值推理、长输出的增量 Dynamo decode block 反馈。正式研究这些功能前必须补齐对应机制。

因此初始实验建议保留 `output_tokens < block_size`。当前 priority 模式仅是可修改策略的例子，不是完成的动态存储感知策略。Dynamo 相同代价下仍可能随机选 worker，复跑可比较总体指标，不保证逐请求落点完全一致。

修改真实调度源码后直接重跑；`scripts/doctor.py --allow-edited` 会报告已修改文件。若改了 Ascend 源文件，补丁脚本会要求人工重新审查，不会对未知内容盲目打补丁。

## 常见问题

- `Failed to import from vllm._C`、Triton/NIXL 不可用：在本工程 empty-device、仅调度核心模式下是预期提示；doctor 和集成断言通过才代表可用。
- 出现 `torch_npu` / CANN 初始化：检查是否用了其他环境、启用了全量插件，或新增代码导入了设备路径。不要用 MagicMock 掩盖此错误。
- `deadlock: no future event`：先检查 block 容量是否足够容纳活跃请求、是否正确处理 connector completion；此错误不会伪造完成结果。
- 下载失败：检查到 PyPI、download.pytorch.org、codeload.github.com 的网络，按本地要求设置代理。固定 SHA 源码和已安装依赖可复用，重复运行不会覆盖来源不明的目录。
