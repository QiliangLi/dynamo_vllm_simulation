# Dynamo + vLLM / Ascend 的 CPU 调度仿真启动包

本工程已经跑通真实 Dynamo SelectionService、vLLM 0.20.2 Scheduler / KVCacheManager，以及按文件加载的 Ascend 0.20.2rc1 BalanceScheduler。NPU 计算和远端存储由事件驱动模型代替。**这是可运行的调度核心实验台，不是完整 Dynamo 服务 + vLLM EngineCore + 全量 Ascend 插件的 CPU 移植。**

详细设计见 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)，实测和适用范围见 [VALIDATION.md](VALIDATION.md)。

## 直接启动

已验证平台：Linux x86_64、Python 3.12、CPU；macOS 26 Apple Silicon 亦已验证（需按下文适配，2026-09-24，见 VALIDATION.md）。建议使用独立机器/容器或 WSL2。需要能够下载 GitHub 源码、PyPI 包及 CPU PyTorch；无需模型权重、CUDA、CANN、NPU、etcd 或 NATS。初次准备建议预留数 GB 磁盘空间。

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后：

```bash
unzip dynamo_vllm_cpu_sim.zip
cd dynamo_vllm_cpu_sim
bash scripts/bootstrap.sh
```

脚本会按 SHA 下载三个上游仓库、建立 `.venv`、安装固定依赖、以 `VLLM_TARGET_DEVICE=empty` 安装真实 vLLM Python 源码、检查来源并跑完 12 个请求。结果在 `results/bootstrap/report.json` 和 `events.jsonl`。脚本会拒绝覆盖来源不明的已有源码目录。

若环境使用代理，按当地网络要求设置 `HTTPS_PROXY` / `HTTP_PROXY` 后运行；项目不会自行修改系统网络设置。启动后不需要联网获取模型。

### 在 macOS（Apple Silicon）上运行

bootstrap.sh 的固定安装路径在 macOS 上有三处不适用，需手动适配（2026-09-24 在 macOS 26.6 / ARM 验证，五模式全部跑通，指标与 Linux 逐位一致）：

1. **torch**：PyTorch CPU 索引不为 macOS 发布 `+cpu` 后缀 wheel，改装无后缀同名版本 `uv pip install --python .venv/bin/python --no-deps 'torch==2.11.0'`。副作用：`scripts/doctor.py` 的 torch 精确版本检查（要求 `2.11.0+cpu`）在 mac 上会失败，属预期偏差；仿真与集成断言不受影响。
2. **ai-dynamo-runtime 1.5.0**：PyPI 只发布 Linux wheel，需从已下载的固定 SHA 源码自行编译，且必须带 `select-service` feature（默认 feature 不含 `SelectionService`）：
   ```bash
   uv pip install --python .venv/bin/python maturin   # 另需 brew 的 rust/protoc
   cd upstream/dynamo/lib/bindings/python
   SDK=$(xcrun --show-sdk-path)
   CXXFLAGS="-isystem $SDK/usr/include/c++/v1" <venv>/bin/maturin build --release \
       --features select-service --out /tmp/dynamo-wheels
   uv pip install --python <venv>/bin/python --no-deps /tmp/dynamo-wheels/*.whl
   ```
   `CXXFLAGS` 是本机 Xcode CommandLineTools 的 libc++ 头损坏时的绕法（vendored ZeroMQ 编译报 `'new' file not found`）；根治需重装 CLT。
3. **vLLM**：0.20.2 的 `setup.py` 在 darwin 上强制把 `VLLM_TARGET_DEVICE` 覆盖为 `cpu`（触发 C++ 扩展编译）。本工程已将该文件改为：显式指定 `empty` 时不覆盖（macOS 默认仍为 `cpu`）。这是一处对上游源码的有意修改；`setup.py` 不在 `source-fingerprints.json` 内，doctor 溯源不受影响，重跑 bootstrap 会因 upstream 目录已存在而保留该修改。

其余步骤（`fetch_sources.py`、lock 依赖 `--no-deps` 安装、editable vLLM、`sim.run`、测试）与 Linux 相同。

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

# 存储带宽分配策略对照：max_min_fair（按需水土填充，无 Path 排队）
.venv/bin/python -m sim.run --bandwidth-policy max_min_fair --output results/max_min_fair
.venv/bin/python -m sim.run --config configs/slow_storage.json --bandwidth-policy max_min_fair --output results/max_min_fair_slow

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_results.py results/ascend_default results/upstream results/priority results/slow results/ascend_balance results/max_min_fair results/max_min_fair_slow

# MPC 预测模型可行性验证：仿真世界（真实 vLLM Scheduler 对象 + Storage）可整体深拷贝，
# 且拷贝世界在相同驱动下与原世界轨迹逐位一致——rollout-by-deepcopy 的前提断言
.venv/bin/python scripts/verify_rollout_copy.py
```

`ascend_balance` 补丁只针对本工程复现的异步 KV 状态更新问题，未在 NPU 验证。脚本不改上游文件。该模式为每个 worker 一个 DP rank，不模拟真实 DP collectives。

## 存储模型与带宽分配策略（2026-10-10 起）

块经哈希只落到唯一 ASU（盘），对该盘全部 Path 可见；**Path 不是放置属性而是访问通道**——IO 请求发出时才指派（确定性最少占用、平局取最小下标）。`storage.bandwidth_policy`（CLI `--bandwidth-policy`）选择速率规则，设计文档见 [../docs/存储带宽分配策略设计-maxmin-20261009.md](../docs/存储带宽分配策略设计-maxmin-20261009.md)：

- **`active_split`（默认）**：每 Path FIFO 排队（同盘并发读超过 `paths_per_disk` 才排队）；对活跃队首按 `min(path_Bps, disk_Bps/该盘活跃数)` 分速率，再按全局共享链路等比例缩放。
- **`max_min_fair`**：**无排队**——每个 IO 请求独占一条 Path，数量不设上限（`paths_per_disk` 仅为名义值）；每个读请求携带需求 `d = kv_bytes_per_token / prefill_token_s`（即一层传输数据量/一层计算时间，聚合模型下层数相消；trace 行可选 `demand_Bps` 覆盖，供异质需求实验）。每盘做带需求上限的水土填充：Σd ≤ 盘容量时各得需求 + 剩余均分；否则迭代"均分→超额者截到需求→多余再均分"到不动点。随后同样按共享链路等比例缩放（链路主导时需求保证会被压缩破坏，设计文档 §3.5 有记录）。

两策略的 `snapshot().path_rates_Bps` 键均为 `"盘:Path"`（max_min_fair 的 Path 序号可超过 256）；`storage_interval_log` 的 `queued_reads` 在 max_min_fair 下语义为"未过启动延迟的读请求数"（无排队概念）。2026-10-10 的这次 Path 语义变更（废除旧哈希 (盘, Path) 定位）同时作用于两策略，五模式基线已重跑，见 [VALIDATION.md](VALIDATION.md) 对应追记；此前数值（含 A/B 实验）对应旧定位语义。

## A/B 双类带宽争抢实验（2026-10-08）

把旧框架 E26 的 A/B 双类工况（A=输入 128K/重算 256，每层读 175.65MB/算 6.02ms；B=输入 32K/重算 4096，每层读 38.5MB/算 28.59ms；32 NPU、共享链路 120 GB/s、batch=1、A:B=1:2）移植到本实验台。设计见 [../docs/AB双类带宽争抢实验设计-20261008.md](../docs/AB双类带宽争抢实验设计-20261008.md)，结果文档（含分类甘特图与带宽时序图）见 [../AB双类带宽争抢实验结果-20261008.md](../AB双类带宽争抢实验结果-20261008.md)，验收数值见 [VALIDATION.md](VALIDATION.md) 追记。

**虚拟 token 标定（语义说明，不得隐瞒）**：本实验台计算模型为单一全局线性系数，字面重算 token 数无法同时命中两类计算时间，因此按 E26 大纲 §3 的方法以"每类总读字节 + 每类总计算时间"为绑定量：B 类字面保留（重算 4096、prefill_token_s=55.83984375µs 精确命中 228.72ms）；A 类重算虚拟化为 862 token 命中 48.13ms（字面 256，偏差 −0.05%），prompt 变 131678（+0.5%）。读字节经 `kv_bytes_per_token=10741.744` 由字面前缀长度（A 130816 / B 28672）精确命中（A 1.4052GB、B 0.30798GB）。

```bash
.venv/bin/python scripts/gen_ab_trace.py --order block        # 生成 trace 到 results/traces/（gitignored，~68MB）
.venv/bin/python scripts/gen_ab_trace.py --order interleave
export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1   # 模型 stub 的 max_position_embeddings=4096 < 139264
.venv/bin/python -m sim.run --config configs/ab.json --trace results/traces/ab_block_r2_b1.jsonl --output results/ab_block_b1.0
.venv/bin/python -m sim.run --config configs/ab.json --trace results/traces/ab_interleave_r2_b1.jsonl --output results/ab_interleave_b1.0
.venv/bin/python scripts/check_results.py results/ab_block_b1.0 results/ab_interleave_b1.0
.venv/bin/python scripts/ab_analyze.py results/ab_block_b1.0 results/ab_interleave_b1.0   # 按类指标
.venv/bin/python scripts/ab_fig.py results/ab_block_b1.0 --tag block --audit              # 甘特+带宽图 → ../docs/figures/
.venv/bin/python scripts/ab_fig.py results/ab_interleave_b1.0 --tag interleave --audit
```

`configs/ab.json` 开启了 `storage_interval_log`：events.jsonl 追加 `storage_interval` 事件（每个推进区间的链路需求/实际速率与队列深度，默认关闭、不影响其他实验），带宽时序图与守恒断言（∫actual=bytes_read）的数据源。`ab_fig.py` 依赖 matplotlib（已入 requirements.in/lock）。

首轮结论（192 请求、2 轮、轮距 1.0s、两臂同 config 同 CRN）：两臂读量与 compute 合计逐位一致（129.3547 GB / 32.671s），链路饱和时长=理论排空下限；**E26d 的"块状→洪水、交错→天然错峰"结论不迁移**——真实 vLLM 异步 KV 加载让每 worker 队列内全部请求的远端读立即提交（两臂并发 A 峰值 32/36，链路超订、全程满载），到达序的真实作用点是每 worker 的计算队列次序与 Dynamo 负载分布：块状臂 32 个 worker 首位全是 A（A 先算，makespan 2.208s、A 类 SLO 28.1%），交错臂首位 11A/21B，A 排在 B 的 229ms prefill 之后单调恶化（队列第 0→7 位 TTFT 345→1533ms，makespan 3.112s、A 类 SLO 18.8%）。含义：真实 vLLM 语义下"错峰"无法靠到达序获得（读不受准入控制），需要 admission 类策略——对应检视报告的 P/B 层挂载点缺口。

**复现性注记**：Dynamo 原生 picker 平局随机使同配置复跑的均值类指标漂 0.3~2.1%、最值类（makespan/p95）漂 1.2~10.9%（worker 落点 ~185/192 变化，读量/compute 逐位不变）——正式对照实验前需确定性 picker 或多种子，详见结果文档 §6。

局限：无逐层 I/O（收齐全部块才计算，单条理想 TTFT 比 E26 的 T0 多算读时）；decode_token_s 为任意取值；trace 为合成 token、无真实前缀共享结构。

## 代码入口

| 文件 | 职责 |
|---|---|
| `sim/router.py` | 调用真实 Rust SelectionService；reserve / prefill complete / free 的记账同步 |
| `sim/engine.py` | 构造真实 Scheduler、Request、KVCacheConfig；生成仿真 ModelRunnerOutput |
| `sim/connector.py` | 真实 KVConnectorBase_V1 接口：命中、分配、异步完成 |
| `sim/storage.py` | 共享 KV 池、ASU 内带宽分配双策略（active_split / max_min_fair）、IO 请求发放时指派 Path、动态链路带宽 |
| `sim/run.py` | 全局虚拟时间、事件推进、指标和 JSON 记录 |
| `upstream/vllm/vllm/v1/core/sched/scheduler.py` | 你要修改的真实 vLLM 调度器 |
| `upstream/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py` | Ascend 调度适配源码 |
| `upstream/dynamo/lib/kv-router/src/scheduling/selector/policy.rs` | Dynamo 的真实 Rust 策略接口；修改后必须重编译 Python 扩展 |

依赖分两种：`requirements.in` 是人工整理的导入依赖入口；`requirements.lock` 是验证环境锁定的完整安装集合。启动脚本使用 lock 和 `--no-deps`，这是裁剪设备依赖的 CPU 实验环境，不能作为 NPU 生产安装方式。`pip check` 可能报告未安装的设备相关生产依赖；本工程用实际导入和集成运行验证选定代码路径。

## 输入与输出

每行一个 JSON，请求时间单位为秒、吞吐单位为 bytes/s：

```json
{"id":"request-1","arrival_s":0.0,"prompt_token_ids":[11,12,13,14],"output_tokens":8,"remote_prefix_tokens":0,"priority":0,"demand_Bps":2000000000}
```

`remote_prefix_tokens` 必须为 block_size 的整数倍，声明仿真开始前共享存储中已存在的前缀；同一个前缀对所有请求可见。不要把这字段用于声明一个未来才生成的缓存。真正的命中仍由 vLLM 的内容哈希和连续前缀规则决定。priority 数值越小优先级越高。`demand_Bps` 可选（正有限值），覆盖该请求的推导带宽需求，仅 `max_min_fair` 策略消费；`active_split` 下出现该字段不报错、不生效。

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
