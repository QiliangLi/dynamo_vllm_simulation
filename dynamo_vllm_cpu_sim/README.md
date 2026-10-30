# Dynamo + vLLM / Ascend 的 CPU 调度仿真启动包


标准benchmark运行器从所选负载的config.json/manifest.json读取共享池带宽，并校验名称一致；不将带宽固定为90 GB/s。完整名单及支持状态见[Benchmark目录](../benchmark/benchmark目录.md)。

本分支最新入口：[实验分支使用说明](../docs/实验分支使用说明.md)。
固定输入位于仓库根benchmark，统一运行器为`scripts/run_benchmark.py`。
新增[AgentX360固定输入](../benchmark/agentx_random_360_12s_12_180GBps/README.md)：360条、12秒、180GB/s、12实例。可校验及导出；当前交付运行器尚不支持其直接组合计时、关闭反馈及长上下文合同，显式选择会在启动前拒绝。`--case all`只选择目录中支持完整运行的负载。
先运行`--smoke`，再按说明运行90GB/s、K=1的正式对照。下文按日期保留历史入口与结果。
存储优化测试所需的少量冻结参考源码已放入`tests/fixtures/`，不再依赖历史results目录。

2026-10-27新增可选等待heavy折扣计分。`routing_accounting.waiting_factor`取0～1，启用时缺省0.1；
`--waiting-score-factor`启用并覆盖比例。额度获准后补齐分数，分配失败回滚折扣，无外部命中转普通完整计分。
默认关闭。原生Dynamo保持完整资源booking，只在默认Rust scorer中扣除等待折扣，保留原picker和容量检查。
须独立构建overlay，并用`--routing-python-path`仅为frontend加载；不覆盖已安装wheel或固定上游。
当前限单前端/单模型/aggregated/kv/block=1/无前缀缓存/scheduler冻结准入，不支持热更新或跨机器评分控制。
构建、启动、故障边界与验收见[折扣计分说明](../docs/等待重请求折扣计分与获准补齐-20261027.md)。

2026-10-26 V1顺序调整：仅在`admission.placement=scheduler`模式下，先按已有hint
申请额度，再调用外部KV命中查询。额度不足直接沿原队列逻辑跳过。
提前申请后的查询/预算/分配退出会回滚本次未提交读的额度。
默认gateway路径、分类、额度上限、重试和释放规则、查询协议、存储模型均保持原设置。
此调整已新增重跑ABC一次：平均TTFT772.574ms、完成4298.853ms、SLO44.1667%，
模拟计算占比75.5%。旧查询后V1为2376.837ms/10673.483ms；旧网关TFS为685.452ms/4755.257ms。
新运行每请求KV命中查询一次，共360次；精确数据和限制见VALIDATION。下文原ABC小节保留调整前记录。

## ABC调度前准入V1收益检查（2026-10-26）

`scripts/run_seed416_read_admission.py --name <结果名>`只运行新V1，复用冻结seed416输入、原SA进程栈及细粒度探针。
`--smoke`选择ABC各两条、两个实例。完整运行12实例，60GB/s共享池，K10，8192token/步、4序列，SA八层逐层预取。
依赖`results/seed416_fine_source_20261026/`和原实验适配文件。输出目录须不存在。
本轮仅新增一组正式运行，旧单进程和12进程TFS直接复用历史记录。
V1完成360/360，但平均TTFT和完成时间变差。功能正确性不代表性能优化。
对照设计见仓库根docs下`ABC调度前准入V1收益对照设计-20261026.md`，结果见VALIDATION。

## Mooncake逐层单多进程对照（2026-10-26）

新增独立入口`sim.layer_aligned`和`scripts/run_mooncake96_pair.py`。
两形态复用真实Scheduler、KV分配、Dynamo负载选择、GroupedStorage逐请求逐层读取及GLM计算函数。
该DES入口省去HTTP、IPC和宿主睡眠的虚拟耗时；旧`sim.run`整段读取和SA八层入口保持原行为。
运行前需准备冻结输入、配置及原生栈适配依赖。输入准备和完整驱动快照保存在工作区报告的`reproduce/`目录。

本次conversation前360条、每请求96%命中、4秒到达、120GB/s共享池、12逻辑实例、8192新token/步、4序列。
计算模型保留GLM5/D950PR内部DP/EP=32参考，12实例不代表完成12rank硬件校准。
入口使用`--form des|native --mode immediate|tfs --name <结果名>`；`--smoke`仅用于6请求/2实例验证。
详见`docs/Mooncake96命中四秒单多进程对照设计-20261026.md`（位于仓库根docs）及VALIDATION记录。

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

## 进程化仿真栈（2026-10-21 起）：真实 Dynamo + 真实 vLLM 多进程 + mock 执行

设计文档见 [../docs/多进程真实Dynamo-vLLM仿真栈设计-20261020.md](../docs/多进程真实Dynamo-vLLM仿真栈设计-20261020.md)（含 2026-10-21 模式 1 实现追记）。单进程 DES 保持原样（两形态分工：DES 管定量对照，进程栈管协议正确与在线路径）。

**标准流程是一条命令**：起栈 → 等就绪 → 回放 → 验收断言 → 自动停栈（`trap` 保证失败/中断也停；每次实验独立的日志目录 `/tmp/stack_logs/<name>` 与发现目录，不互相污染）：

```bash
bash scripts/bootstrap_stack.sh                 # 一次性：装 ai-dynamo 组件包(--no-deps)+watchfiles+torchvision 0.26 等
bash scripts/run_stack_experiment.sh --name stack_remote                                    # 远端 KV 命中（8 请求）
bash scripts/run_stack_experiment.sh --name stack_smoke --no-remote-kv --trace configs/stack_smoke.jsonl   # 纯本地（12 请求）
bash scripts/run_stack_experiment.sh --name stack_slow --sim-config configs/stack_slow.json                 # 慢存储对照
.venv/bin/python scripts/check_stack.py results/stack_smoke results/stack_remote results/stack_slow        # 跨实验断言（慢/快比>3×）
```

产物 `results/<name>/report.json`；逐步调度记录 `/tmp/stack_logs/<name>/worker*.steps.jsonl`。需要手动保持栈运行调试时才用底层命令：`STACK_REMOTE_KV=1 bash scripts/launch_stack.sh up 2`（`stop` 收尾；stop 同时按跨运行登记表回收任何 LOG_DIR 不同的历史残留进程）。`vllm serve` 单进程冒烟用 `scripts/stack_llm_smoke.py`。

资源占用（2026-10-21 实测，2 worker 共 6 进程）：空闲全栈 ~0 CPU、RSS 合计 ~0.3–0.5GB（大头是每 Python 进程固定 import 的 torch/vLLM ~40–90MB，而非计算）；mock 执行为 `time.sleep`，无 GPU/NPU 占用；一轮 8 请求全栈 CPU 时间增量 ≈0.09s（≈11ms/请求）。跑完即停（标准流程自动完成）。

要点与边界：vLLM/Dynamo 源码零修改——SimExecutor 经 vLLM `Executor` 限定名注入（`scripts/stack_vllm_worker.py` 包装层同时补齐 0.20.2↔0.28 的两处运行期 API 漂移）；`PYTHONHASHSEED=0` 必须统一（前缀缓存块哈希依赖内建 hash()，跨进程共享池的前提）；token 精确实验必须走 completions（chat 模板会改变 token 序列）；真实 vLLM 容量准入门要求 `num_blocks×block_size ≥ max_model_len`；时间为壁钟（模式 1），µs 级时序保真与确定性由 DES 形态承担。2026-10-21 验收数值见 VALIDATION.md 追记。

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

## 请求级 TFS 准入控制（2026-10-19，tfs_test 分支）

TFS 位于真实 Dynamo `SelectionService` 之前。中央队列先决定请求何时放行，
Dynamo 决定实例，真实 vLLM Scheduler 决定最终 batch。已有请求的实例仍可接收请求。
实现不修改上游代码、模型耗时公式或存储分配公式。第一阶段存储统一使用 `active_split`。
详细规格见 `../docs/TFS接入dv技术方案-20261019.md`。

```bash
.venv/bin/python scripts/gen_tfs_trace.py --preset abc_burst --seed 11 --output results/traces/tfs_smoke.jsonl
.venv/bin/python -m sim.run --config configs/tfs_demo.json --trace results/traces/tfs_smoke.jsonl --admission immediate --output results/tfs_smoke/immediate
.venv/bin/python -m sim.run --config configs/tfs_demo.json --trace results/traces/tfs_smoke.jsonl --admission tfs --release-on first_token --output results/tfs_smoke/tfs
.venv/bin/python scripts/check_results.py results/tfs_smoke/immediate results/tfs_smoke/tfs
.venv/bin/python scripts/run_tfs_matrix.py --spec configs/tfs_matrix.json --output results/tfs_matrix --jobs 4
.venv/bin/python scripts/analyze_tfs.py results/tfs_matrix --figures results/tfs_matrix/figures
```

macOS 可使用本地 `.venv/run-sim` 运行入口。没有该本地便利脚本时，直接调用 `.venv/bin/python -m sim.run`。
矩阵每格使用独立进程，默认 2 个并行进程。`--dry-run` 导出完整配置与清单；
`--resume` 仅复用配置、trace 哈希和仿真实现哈希一致的成功结果。

- `--admission immediate|slack_only|quota_fcfs|tfs|tfs_no_aging`。
  旧 JSON 未配置时使用 `immediate`；`tfs_no_aging` 是 `tfs` + `aging_enabled=false` 的别名。
- `--release-on first_token|load_done`；前者为主口径，后者为敏感性实验。
- `--max-num-seqs`、`--max-num-batched-tokens` 显式覆盖 JSON。
- 画像使用冻结的 `request_profile.profile_token_budget`，改变计算 batch 上限不会改变分类和截止时间。
  `pressure_Bps` 为请求级估计，不能复用存储分配的 `demand_Bps`。
- P75 采用 `floor(q*(N-1))` 下标，仅统计已到达历史，包含已完成请求。同刻全部到达一次更新。
  全零读时无限流；P75=0 但有正读时使用最小正值。元数据记录该 dv 适配规则。
- 所有获准请求都开启租约；门槛改变时重算仍开放租约的重分类，不重开已释放租约。
  受限重请求不会阻塞后面的轻请求。老化每次最多豁免一个，并受固定冷却时间限制。
- 反馈分 `occurred_s` 和 `visible_s`。观测延迟不延迟 vLLM 实际 KV 完成；反馈排空不延长业务 makespan 或 Worker 积分。

报告为 `report_schema_version=2`。原指标保持名称，新增中央等待、引擎侧 TTFT、加载提交等待、
加载历时、按类分位数、画像期限达标率、实际计算 batch 及时间加权分布。
事件 `step_scheduled` 的请求数来自 `SchedulerOutput.num_scheduled_tokens`，不使用 `running` 长度。
旧 `arrival` 保留为派发别名；新分析应使用 `request_arrived` / `request_submitted`，以免遗漏中央等待。

合成 A/B/C 前缀互不共享；生成参数和文件 SHA256 与 trace 一起输出。正式配置固定
5 个 trace 种子、每 trace 重复 3 次、3 个负载条件、4 个 seq 上限、4 个策略，共 720 格。
种子只决定负载，不能消除 Rust 同代价选择的随机平局。分析先取种子内重复中位数，再取跨种子中位数；
同时保留逐次、逐种子和配对差值。`classes` 指标中的样本少于 100 标记 `small_sample`。

抢占或重复加载会标记 `main_experiment_valid=false`，分析将整组对照标为超出主实验假设，
不能只剔除表现较差的策略。KV blocks 采用统一充足容量；实际是否发生多请求 batch 必须查看输出分布。
`stall_s` 仍为所有未完成但无计算在途的聚合时间，不是纯 IO 等待。
固定参考带宽不等于动态带宽观测；整体加载、线性计算模型不能替代逐层读算或真实 GPU 测量。

第三阶段释放/观测敏感性规格为 `configs/tfs_sensitivity.json`。
它复用第二阶段已生成的 burst trace（因此须先运行主矩阵），固定 `max_num_seqs=8`，
比较 first_token/load_done、启用/禁用老化、0/1/5ms 反馈，共 180 格。
分析器输出对应配对差值，不把敏感性变体混进主矩阵的 TFS 主口径。

生成器还接受 `--parameters <JSON文件>`，覆盖完整 preset 中的负载字段，例如
`unread_c`（C 的合成计算 token 数）、`interval_s`、A/B 的远端长度和未命中长度。
未知字段会报错；最终实际参数写入 `.meta.json`。同一个实验必须复用生成好的 trace，
不能为不同策略单独改变这些参数。

## SA E26f 精确输入与逐层模型对照（2026-10-19）

本轮替代旧 24 请求验证负载，直接从固定 SA 源码导出官方 C_A seed 416/417/418、C_B seed 424。每份 360 请求，h/u/到达/T0/期限逐字段保留；不使用旧 A/B 的虚拟 token 映射。32 Worker、8 层、8192 token/step、120 GB/s 聚合池、q_max=200 GB/s，计算公式及系数与 SA E26_PROFILE 相同。

新增 `sim.sa_aligned` 入口，原有 `sim.run` 默认行为不变。`SALayerConnector` 使用真实 vLLM 同步层加载协议（async=False）；真实 Scheduler 决定 step 成员和 token，仿真 worker 在层读取完成后计算，并在本层计算开始时预取下一层。存储 `sa_max_min` 对整批每层流分配，不是已有逐块逐盘 `max_min_fair`；其欠载剩余均分、过载 max-min、到期升至 q_max 均与 SA 一致。

逻辑 block_size=1 精确保留 h=42883/9399，关闭本地前缀复用；无真实数值设备执行，不宣称 GPU 支持该块配置。每请求输出 1 token，完成 prefill 后结束，避免额外 decode。逻辑 8GB 内存按 SA 的 base=0.5GB、KV=8×4096 bytes/token、workspace=131072 bytes/计算 token 核算；KV pool 预留最大 8192-token workspace，所有格点共用同一容量。每格审计实际峰值和无抢占。

```bash
.venv/bin/python scripts/export_sa_e26f.py \
  --sa-root ../../status_aware_network --output results/sa_alignment_20261019/source
.venv/bin/python scripts/verify_sa_alignment.py --output results/sa_alignment_20261019
.venv/bin/python scripts/run_sa_alignment.py --output results/sa_alignment_20261019
.venv/bin/python scripts/analyze_sa_alignment.py \
  --output results/sa_alignment_20261019 \
  --archive results/sa_alignment_summary_20261019 \
  --figures ../docs/figures/sa_alignment_20261019
```

导出器在独立进程导入 SA，运行 8 次 SA FCFS/TFS 现场控制；保存 commit/源码 SHA 与逐请求输出。验证器运行四个 singleton、两个单 Worker 串行和两个双 Worker 并发对照，与独立 SA 引擎逐请求比较。矩阵固定四份 trace × 两档 max_num_seqs(1/8) × 四种策略 × 三次路由重复=96 格；`--dry-run` 只展开，`--resume` 仅重用实现 SHA/配置相同且重验收通过的格点。矩阵顺序运行，任何失败立即停止，不按策略成绩改参数。

有意变化是 Dynamo 路由、每实例队列、vLLM 自主组批/chunked prefill；TFS 的重请求租约包括实例内等待，不能假称与 SA 的单活动批完全相同。max_num_seqs=1 仍保留真实 Dynamo 排队，不等于强制按 SA 的空闲 Worker 派发。C_A 三输入种子取中位数，C_B 仍只有官方一个输入种子；三次路由重复不算三个输入种子。控制器不访问未来输入或存储真值。设计和正式结果见仓库根目录文档链接。

### 离线 HTML 实验报告

`SA-E26f对齐实验报告-20261019.html` 位于仓库根。结构参考原 A/B 实验结果和 FPM HTML：结论、参数与口径、策略定义、汇总与逐种子、计算/带宽图、机制、局限和复现。可按 C_A/C_B、请求数上限、官方输入种子和指标核对数据；完整16组汇总、48行分类与96次运行表始终保留。

```bash
.venv/bin/python scripts/build_sa_report_html.py
```

生成器仅用 Python 标准库，从已验收的 `results/sa_alignment_summary_20261019` 和正式 PNG 读取，不运行仿真、不改数据。模板为 `scripts/templates/sa_report.html`。单文件内嵌四幅原图、全部交互数据、CSV/逐请求CSV.gz、SA原始输入与对照ZIP及完整Markdown原报告，不依赖网络或CDN。提供深浅主题、图表放大与打印样式。手机宽表在容器内横向滚动。

### TFS 与 SA 对齐代码审查

仓库根 `TFS与SA对齐代码改动说明-20261019.html` 固定比较 `a870596 → 344f623 → e6a0825 → 8ec666f`。报告包含 30 个源码/配置/脚本/测试文件的原文与阶段差异，以及全部 208 个变更文件的清单和 SHA256。可按阶段查看原文、查找符号、下载补丁。报告明确区分真实 Scheduler/Router 与合成执行模型，解释同步 Connector、逻辑 block=1、请求级配额与 SA 在跑批次计数的差别。

```bash
.venv/bin/python scripts/audit_sa_code_review.py
.venv/bin/python scripts/build_sa_code_review_html.py
```

审查脚本读取本地完整 `results/sa_alignment_20261019` 原始结果、既有精简归档、固定 Git 提交和 SA 源；需要这些文件及当前固定环境。它核对 384 个原始文件指纹，重新调用 96 次验收器，独立重算请求指标、双层中位数、准入排序与逐步画像，不重新运行正式仿真。结果写入新的 `results/sa_code_review_20261019`。该目录的 `unit-tests.log` 和 `runtime-imports.json` 是本轮现场测试与导入路径检查记录，脚本会核对其状态/当前文件指纹；重新做现场检查时应更新这些记录，不能把旧记录称作新运行。

HTML 构建器只读取固定提交和审查证据；模板为 `scripts/templates/sa_code_review.html`。本次没有修改 `sim/`、实验配置、原测试或既有实验归档。审查支持数据与轨迹一致性，不证明所有收益变化仅由组批导致，也不证明硬件性能。独立多请求 batch 的端到端对拍仍待补充。

### Archify 合并报告

仓库根 `TFS代码改动与实验报告-Archify-20261020.html` 将代码改动放在前半部分，实验结果放在后半部分。它由 Archify 3.2.0 原生 explainer 渲染，包含调用架构图、step 交互顺序图、逐步阅读、源码卡片、两幅数值图和三幅归档图片。16 行策略、48 行分类、96 行运行记录完整保留；208 个变更文件及 SHA256 以完整 CSV 代码块保留。可从第一节跳转到实验和策略结果。

```bash
.venv/bin/python scripts/build_archify_tfs_report.py \
  --folder ../.archify/explainer-tfs-sa-20261020-090731
cd ..
node "$HOME/.codex/skills/archify/bin/archify.mjs" finalize explainer \
  .archify/explainer-tfs-sa-20261020-090731/candidate.archify.md \
  TFS代码改动与实验报告-Archify-20261020.html \
  --repo-root "$PWD" \
  --out-dir .archify/explainer-tfs-sa-20261020-090731/receipts/new-review --json
```

需先安装 Archify skill 和 Node.js 18 或更新版本。生成脚本仅用 Python 标准库；读取已有归档，独立重算全部 34,560 条请求对应的 96 次指标和双层中位数，写出 Markdown、图表 CSV 与图片副本。默认新建带上海时区时间戳的 `.archify/` 目录，`--folder` 可重建指定草稿。代码摘录固定到 `607c7eb`，由 Archify 从 Git 提交读取。本次只重新编排与核对归档，没有新跑仿真或测试。

最终 Archify 验收在 `.archify/explainer-tfs-sa-20261020-090731/receipts/review-3/`，现场浏览器记录为同目录上层 `browser-qa.json`。Explainer 的 finalize 不自动启动浏览器；独立 Chrome 检查与工具门禁分别记录。公共链接可能无法访问尚未推送的本地提交，但图表、图片与代码摘录已内嵌，可离线阅读。

### 五色时间线（2026-10-20）

执行 `.venv/bin/python scripts/plot_sa_five_state.py` 可从四份已归档轨迹生成仓库根 `SA-E26f五色时间线-20261020.md` 和三幅 PNG。蓝/橙/绿分别表示 A/B/C 计算，红表示执行 step 内的 IO 等待，灰表示 worker 无请求。混合类别采用同一时间区间内上下分条，不代表算力占比。脚本逐 worker 核对三项时长与原报告，另生成 C_A 前 200 ms 按启动顺序排列的放大图。不会重跑仿真或修改原轨迹。


## GLM5 prefill 时间模型接入（2026-10-22）

`compute.mode` 可选 `linear`（旧默认）或 `glm5_d950pr`。DES `Engine` 和多进程
`SimExecutor` 共用 `sim/compute_time.py`。真实 Scheduler 决定每步形状，计时模块不改变
组批、KV 分配或结果回写。来源源码固定于 SA `79f48a0`，副本及 SHA256 位于
`sim/timing_models/`，不依赖运行时访问 SA 仓库。

新模式只支持 prefill-only，trace 的 `output_tokens` / HTTP 的 `max_tokens` 需为 1。
它没有实现完整 PD 移交协议。配置示例：

```json
"compute": {
  "mode": "glm5_d950pr",
  "sparse_HBM_hit_ratio": 1.0,
  "engine_overhead_s": 0.0
}
```

每轮按真实 `(h,u)` 输入 `(h+u,u)`，先做来源工具的异构 padding 上界，再计
`(3*T_dense_us + 75*T_moe_us)*1e-6 + engine_overhead_s`。不重复加旧线性开销。
每批最多 8 个请求；来源布局仍为 `dp=ep=max(1,32//bs)`，它是参考模型的布局，
不是本机启动 worker 数；不模拟每步改变并行布局的重分片。新模式的绝对时间不是
实测性能。GLM5 实际融合、KV 复用率和 0.8 效率仍待目标设备校准。

DV 外部存储独立计时；新模式默认 hit=1 以关闭来源公式额外的稀疏存储拉取，避免
与已接收的完整前缀重复计时。HBM 访存保留。显式设 hit=0.8 可恢复来源工具默认值。
TFS 的到达时参考画像同步用本模型，默认 `request_profile.mode=glm5_reference_v2`；
它只读请求公开长度和参考 chunk 预算。该次 prefill 接入时尚无多进程 TFS；本分支后续接入见下节。

复现少量 Mooncake 长度样本（在本包目录，环境须已完成 bootstrap_stack；输出目录须不存在）：

```bash
.venv/bin/python scripts/validate_prefill_integration.py   --source /path/to/conversation_trace.jsonl.hit.jsonl   --output /tmp/prefill-integration-new-run   --stack-python "$PWD/.venv/bin/python"
```

不传 `--stack-python` 只跑 DES。验证脚本保留来源行号/哈希，但因原 trace 无文本，
合成隔离 token 前缀；保留长度和派生命中量，改为同时到达、输出 1 token。
它不代表原 trace 的完整服务性能回放。`formula_check.json` 逐步独立复算算子公式，
`timeline_check.json` 比较独立业务完成时间；生产函数不被复算器导入。
多进程日志另记 `sleep_elapsed_s`，不能把 HTTP 延迟或起栈耗时当成模型时间。
设计见 `../docs/prefill_time接入与公式对拍-20261022.md`。

本次实际结果和可读手算见 [prefill_time接入验证结果-20261022.md](../prefill_time接入验证结果-20261022.md)。


## 多进程 TFS 准入（2026-10-22，已实现）

当前真实请求路径为 `客户端 → TFS HTTP 服务 → Dynamo frontend → worker/EngineCore`。
独立服务 `sim/stack/admission_service.py` 复用既有 AdmissionController，依据到达时
公开画像做 slack 排序和重请求配额控制。Dynamo 继续选择实例，vLLM 继续自主组批，
不要求 worker 空闲，不把 batch 限制为 1。`immediate` 对照也经过相同代理。

EngineCore 经 `SIM_STACK_ADMISSION_SOCKET` 回传实际加载提交、领取 KV 完成和首次计算
无需外部加载的事件；代理观察真实内容 token 后回写 first_token，正常响应结束后回写
request_finished。默认首 token 释放；可选 KV 完成释放。请求事件 ID 去重，未登记的
KV 完成或错误 worker 绑定会失败。控制器不读取引擎队列、存储剩余量或未来 trace。

当前入口限定 `/v1/completions`、文本 prompt、stream=true、max_tokens=1。客户端扩展
`_sim_admission` 包含 id、estimated_remote_tokens 和可选 class_id；代理转发前移除。
预计远端命中是公开仿真假设，实际命中/字节由引擎反馈独立记录。底层使用固定版本
支持但已 deprecated 的 `x-dynamo-request-id` 做关联，不要求修改 vLLM/Dynamo 源码。
生产环境适配不在本次范围。

一键运行（需完整 stack Python 环境；输出名称必须是新的）：

```bash
bash scripts/run_stack_experiment.sh --name tfs-prefill-new --admission tfs
bash scripts/run_stack_experiment.sh --name immediate-prefill-new --admission immediate
bash scripts/run_stack_experiment.sh --name tfs-load-new --admission tfs --release-on load_done
```

默认使用 `configs/mooncake_first_seen.json` / `.jsonl` 的 6 请求首现命中样本。`STACK_PYTHON`
可指定完整环境解释器。也可直接运行：

```bash
.venv/bin/python scripts/run_tfs_stack.py --config configs/mooncake_first_seen.json --trace configs/mooncake_first_seen.jsonl --output /tmp/tfs-prefill-new --mode tfs --release-on first_token --workers 2
```

旧 8 请求功能矩阵复现：`scripts/validate_tfs_stack.py --source /path/to/conversation_trace.jsonl.hit.jsonl
--output /tmp/tfs-matrix-new --python /path/to/full-stack/bin/python`。默认起两个真实 worker、
两个 EngineCore、frontend、storage 和 TFS 服务；端口固定为 28765/28766 和 29111 起，
需顺序运行。脚本只回收自己创建的进程组。旧的无 `--admission` 启动方式保持原入口。

客户端断开后，已放行的上游流继续排空并按事实释放租约；排队中可取消。无法确认
上游完成的错误会停止新准入并保留在途租约，不虚构完成。当前不是通用容错网关。
服务使用单调时钟；StorageService 也改用单调时钟推进，并记录实际提交/传输字节和
传输起止，以校验守恒及共享带宽下界。epoch 时间仅用于日志。

## 多进程三盘 HRW 存储（2026-10-23）

此前接入的三盘模式为 **3 块盘，每盘 40 GB/s**，现保留作对照；当前默认见末节统一池。
三盘模式使用 `bandwidth_policy=max_min_fair`、`shared_link_Bps=null`。各盘独立分配
带宽，空闲盘不能代读热点盘上的块。旧显式数值链路配置仍可
用于历史对照。`validate_tfs_stack.py` 继续读取旧 `tfs_stack_prefill` 配置，`--config` 可覆盖。

KV 块使用真实 vLLM content hash，以 HRW（最高随机权重）一致性哈希落到唯一盘：
`argmax_d sha256(block_hash + disk_id.to_bytes(4, "little"))`。所有 worker 共用
storage-sim 的同一放置规则。旧 `tfs_stack_prefill` 配置块大小为 16 token；SA CQ 的默认 512 token
和字符串哈希键不直接移植。算法一致，不宣称两仓库产生逐块相同的落盘结果。

GLM5 计时模式的 Connector 将请求总需求 `V/T` 除以实际读取块数，再交给逐块分配器。
每盘重算活跃块速率；该请求全部块完成后，storage 才向对应 worker 返回完成。
Executor 领取完成后继续走原有 KV 反馈和真实 Scheduler 恢复路径。

`admission.reference_bandwidth_Bps=120e9` 仅用于 TFS 的标量画像和配额参考，
不参与存储分速率，也不是逐盘感知调度。没有共享链路且未显式配置参考值时，
参考值取盘数乘单盘带宽。TFS 本次不新增盘向量观测或决策规则。

`storage.final.json` 记录逐盘提交字节、传输字节及峰值速率。`record_reads=true`
额外保存实际块哈希、请求盘字节向量和物理完成时间，适合短实验；长实验可关闭以
限制内存占用。验收独立复算 HRW，核对实际 RPC 块与 trace 前缀，逐盘检查字节守恒、
40 GB/s 上限及最慢盘完成下界。当前仍是完整前缀只读仿真，不包含真实磁盘 I/O、
逐层预取、写回、动态扩盘或跨盘迁移。

实测与复现见 [三盘一致性哈希多进程接入验证-20261023.md](../三盘一致性哈希多进程接入验证-20261023.md)。

原始记录在 `results/tfs_stack_20261022/`，包括三组各 8/8 成功、准入事件、worker 分配、
实际 batch、27 批独立公式、字节守恒及带宽下界检查。详见仓库根
[多进程TFS接入验证-20261022.md](../多进程TFS接入验证-20261022.md)。

## Mooncake 首现命中（2026-10-23）

默认准入入口现按 first_seen 设计运行。`sim/mooncake.py` 对每个完整原始文件独立
预扫：同刻请求互相不可见、请求内重复不可见、首个未命中终止连续前缀、尾块参与，
全命中保留 1 token。生成 `hit_tokens/u_tokens/full_hit_adjusted`，然后才选择行或窗口。
不同策略消费相同固定 h/u。不使用“全部命中”或 frozen 开关。

```bash
# 原始文件不改。先预扫完整文件，再导出原始行号指定的样本。
.venv/bin/python scripts/prepare_mooncake.py \
  --source /path/to/conversation_trace.jsonl --output /tmp/mooncake-prepared-new \
  --lines 1 2 14 17 262 323 --time-scale 0.001

# 检查派生文件与原文件重算结果逐字节一致，不重写文件。
.venv/bin/python scripts/prepare_mooncake.py \
  --source /path/to/conversation_trace.jsonl \
  --check-only /tmp/mooncake-prepared-new/conversation_trace.jsonl.hit.jsonl

# 运行自己的导出配置和 trace；两个文件必须配对。
.venv/bin/python scripts/run_tfs_stack.py \
  --config /tmp/mooncake-prepared-new/config.json --trace /tmp/mooncake-prepared-new/trace.jsonl \
  --output /tmp/mooncake-run-new --mode tfs --release-on first_token
```

Mooncake 未提供原始 token。导出器使用可往返 tokenizer 的合成文本，保留长度、h/u、
原始行号、hash_ids 和到达分组；不宣称重建真实文本与原始 KV 内容共享。示例把到达
间隔缩放为原来的 0.001 倍，源文件 timestamp 和同刻分组仍保留，输出固定为 1 token。

为精确保留 `h=n-1/u=1`，first_seen 配置使用 **1-token 仿真缓存块**，关闭本地
prefix caching，并扩大逻辑槽数。此前 16-token 配置与历史结果保留。块粒度改变了
HRW 倾斜和逐块公平的输入，不能直接比较两个配置的性能；不是物理 GPU 配置建议。

准入服务在 HTTP 转发前，把 UUID、源行、prompt 和固定 h 绑定到 storage-sim。
Connector 只查该请求允许的前缀；物理预置池里存在更多共享块也不能扩大 h。绑定只
传递固定输入，不向策略提供实时存储状态。缓存数据仍须经历配置的存储读取和完成反馈，
全部块完成后才能继续计算。此模型不模拟先前请求写回完成、容量淘汰或逐层预取。

新矩阵：`scripts/validate_first_seen_stack.py --trace-dir /path/to/mooncake_trace
--output /tmp/first-seen-check-new --python /path/to/full-stack/bin/python`。它核对三份
完整派生指纹，再运行冷/部分/尾块全命中样本的三种模式，验证实际字节、chunk h/u、
公式和租约。Unix socket 使用独立短目录，长结果路径也能启动，结束后回收。

旧 8 请求样本仍可显式传 `--sim-config configs/tfs_stack_prefill.json
--trace configs/tfs_stack_prefill.jsonl`。完整实现边界见
[接入设计](../docs/Mooncake首现命中多进程接入设计-20261023.md)，实测见
[接入验证](../Mooncake首现命中多进程接入验证-20261023.md)。

八实例容量冒烟已在本机实际运行：一个 frontend、八套 worker/EngineCore，24 请求
全部成功，每实例三条，PSS 采样峰值约 8.16GiB。测试包括 TFS 和三盘存储模拟，
不加载实际模型权重。短测试不代替长期压测。资源口径、首轮验收脚本问题与固定种子
复验见 [单Dynamo八vLLM冒烟验证-20261023.md](../单Dynamo八vLLM冒烟验证-20261023.md)。

## 当前默认：统一 120 GB/s 带宽池（2026-10-23）

`configs/mooncake_first_seen.json` 现设 `disks=1`、`disk_Bps=120e9`、
`shared_link_Bps=null`、`bandwidth_policy=max_min_fair`。这个单一逻辑资源表示整个
聚合存储池，不表示真实单块设备的规格。所有读取共用同一个逐块 max-min 分配器，
总容量 120 GB/s。HRW 只有编号 0 可选，哈希倾斜不再形成独立的盘瓶颈。

这复用现有单逻辑盘能力，未改 `Storage` 内核。仅把三盘共享链路设成 120e9 会保留
每盘 40 GB/s 的约束，不能得到此模型。`path_Bps` 在 max_min_fair 模式不参与分配。
first_seen、prefill 时间公式、启动延迟、TFS 及 KV 完成反馈均保持。

`prepare_mooncake.py` 默认继承当前 `mooncake_first_seen.json`，新导出也采用统一池。
如需原三盘 first_seen 配置，显式传 `--sim-config configs/mooncake_first_seen_three_disk.json`
及 `--trace configs/mooncake_first_seen.jsonl`。导出器对应使用 `--config` 指定该三盘配置。
旧 `tfs_stack_prefill.json` 仍是 16-token 三盘历史对照。历史结果保持原配置，不改写。

## 八实例 Mooncake 共享池 TFS 对照（2026-10-23）

复现脚本为 `results/tfs_vs_immediate_8w_20261023/reproduce.py`。它复用现有起停、
回放和验收入口，不修改调度或模拟执行代码。固定 conversation trace 最初132条，
覆盖45秒原始到达窗口；完整首现预扫后选行，补齐末尾同刻组。每次一个 Dynamo、
8套 worker/EngineCore、统一120GB/s池。immediate 与 tfs 各运行三次，交替次序。
immediate 保留公共代理和命中绑定，仅关闭 TFS 排序与配额。均为单输出 token。

在本机 WSL 完整栈环境执行：

```bash
DV_COMPARE_OUTPUT=/tmp/dv-8w-comparison-new \
  /home/zed/.local/share/state-aware-network/audits/dv-process-20261022/venv/bin/python \
  results/tfs_vs_immediate_8w_20261023/reproduce.py
DV_COMPARE_OUTPUT=/tmp/dv-8w-comparison-new \
  /home/zed/.local/share/state-aware-network/audits/dv-process-20261022/venv/bin/python \
  results/tfs_vs_immediate_8w_20261023/analyze.py
```

输出目录必须不存在。脚本内完整栈解释器路径对应本机既有环境；其他机器需配置同版本
环境和该路径。分析脚本将汇总写入自身所在结果目录。实验设计见
[八实例共享池TFS对照设计](../docs/八实例共享池TFS对照设计-20261023.md)。

六次运行已完成，792/792请求成功。三次平均TTFT中位数：immediate=5.803s，
tfs=5.919s。当前窗口存储在途峰值1，TFS配额未触发；本轮没有体现整体延迟收益。
逐次指标、控制变量、证据和限制见
[Mooncake八实例共享池TFS对照结果](../Mooncake八实例共享池TFS对照结果-20261023.md)。
结果目录含原始证据归档、SHA256清单和 comparison.png。需重建归档和结果说明时，
在上述分析完成后，以相同 DV_COMPARE_OUTPUT 执行该结果目录的 `finalize.py`。

## Mooncake 360条、约两秒到达实验（2026-10-23）

`results/mc_360_2s_8w_20261023/run_comparison.py` 取完整首现预扫后的前360条，
将原125.999秒到达跨度等比例缩放为0.1秒，不额外补齐末尾同刻组。
1 Dynamo、8 worker/EngineCore、120GB/s统一池，immediate/tfs各3次。
输入保持原长度和命中量，每轮读取65,257,530,624B；模型计算仍可能超过100秒。
2秒指到达窗口，不是全部完成时间。脚本分别核对客户端派发和网关接收跨度。

结果目录的独立网关适配器预先验证并缓存固定输入的分词结果，同时把客户端和
网关上游池设为512连接、600秒超时。存储传输适配器把AF_UNIX listen队列设为1024，
避免原16队列在突发绑定时EAGAIN。两组共用适配，不修改sim/scripts或默认配置。
第一轮原16队列失败记录保留；正式输出为本机Linux结果目录
`/home/zed/.local/share/state-aware-network/results/dv/mc-360-2s-8w-20261023-v5`。
前四次预试的传输失败或实际到达偏差记录保留。用户明确接受大致接近两秒，
正式首臂实际派发0.978秒、网关登记2.482秒，保留测量偏差，不声称严格两秒。
网关预计算并核对固定画像，只在到达后送入控制器；命中绑定元数据改为已验证的
源请求引用，存储端展开同一输入后走原绑定校验。两组共用，KV字节和策略不变。

使用完整栈解释器执行该结果目录的run_comparison.py、analyze.py、finalize.py，
新运行需通过DV_COMPARE_OUTPUT指定不存在的输出目录。设计见
[360条两秒到达对照设计](../docs/Mooncake360条两秒到达对照设计-20261023.md)。

正式六次已完成，2160/2160成功，实际登记窗口1.787–2.482秒。三次平均TTFT中位数
immediate=73.697s，tfs=68.556s；重请求配额未触发，不将改善归因于带宽限流。
结果见 [360条约两秒到达对照结果](../Mooncake360条两秒到达对照结果-20261023.md)。
`preflights.tar.gz`与对应清单保留未纳入对照的预试，正式数据位于`raw.tar.gz`。

2026-10-23 换机归档：本轮多进程原始结果目录通过外层工作区的 `artifacts/`
压缩分片保存，不将超过 Git 单文件限制的 `raw.tar.gz` 直接提交到 DV 分支。
从外层运行 `python3 tooling/workspace.py init` 或 `start` 可恢复原路径。
读取并发问题及诊断证据见外层 `context/当前进度.md` 与
`temp/storage-submit-probe-20261023/`。问题尚未修复，当前读取并发为 1
不能直接解释为生产环境没有带宽竞争。本次保存未重跑完整实验。


## 存储提交分段诊断（2026-10-24）

新增独立入口 `scripts/diagnose_storage_submit.py`。它只在诊断子进程中包装现有
`Service`/`Storage`；默认服务、块大小、放置、带宽、时钟推进和 TFS 规则均不改。
诊断输出不能用于生产硬件定标，也不是一次 Dynamo/vLLM 全栈重跑。

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/diagnose_storage_submit.py \
  --output results/storage_timing_new
.venv/bin/python scripts/analyze_storage_diagnostics.py results/storage_timing_new
```

输出目录必须不存在。可用 `--observations /path/to/observations.json.gz` 复核已保存
的 360 条实验读取区间。默认 8 客户端、每请求 512 个 1-token 块、9 个工况、每工况
3 次。诊断每请求需求固定100MB/s，不使用正式实验逐请求GLM画像。
RPC 工况使用一个真实服务进程和八个独立客户端进程，计算/vLLM 引擎不启动。
命中绑定与进程启动在测量窗口之外；`rpc_query_submit` 才把真实命中查询计入窗口。
合成读取没有真实 KV 张量。Unix socket 是本机 IPC，不是远端网络基准。

对照包括原始处理器、粗粒度计时包装、关闭 `record_reads`、预先完成64次请求后
保留的空队列，以及查询后提交。各次从新服务开始；交替工况顺序并保留逐次值。
计时仅包围粗粒度调用，不对每块打时间戳；原始事件先放内存，试验结束后写盘。
关闭读取日志只用于诊断，不代表已经采用的修复。

输出 `report.json`、`events.jsonl`、配置和实际运行源码；分析器校验源码摘要并输出
`analysis.json` 与 `summary.csv`。主表为三次运行内部中位数的中位数；分析文件另含
逐请求样本汇总，两个口径不能混加。`cProfile` 只用于调用次数及热点定位，其耗时
不能与未 profile 的运行混用。`rpc_residual` 包含通信、唤醒、调度和客户端开销，
不等于纯网络延迟。服务端完整帧时间是用户态观察时间，不是内核收到数据的时间。

本机结果在 `results/storage_timing_20261024/`。27次小试均通过字节守恒检查。
RPC新服务的模型读取中位数0.569ms、服务端提交1.307ms、客户端调用6.929ms；
64次历史请求留下32768条空队列后，提交2.903ms、客户端调用26.545ms。
原始RPC处理器与计时包装的三次并发峰值均为1。已有六轮正式实验的读取提交间隔
中位数38.81–51.86ms；旧日志缺少分段计时，不能将其全部归因于本次测出的某一项。
完整证据、计时扰动与平台限制见包内 `VALIDATION.md` 末节。

## 存储并发第二步：等价实现优化（2026-10-24）

本轮修改 `Storage` 与 `Service.op_submit` 的实现开销：

- `max_min_fair` 在读取完成后删除空路径。路径序号继续递增，不复用旧编号。
  `active_split` 保留数量有界的空路径，避免改变路径复用时的同刻完成顺序。
- 单盘直接返回盘0；多盘继续使用原 HRW 哈希及二进制编码。
- 读取历史日志复用提交前后的逐盘字节计数，不再第二次遍历块计算放置。
- `advance()` 在同一次调用内复用带宽分配结果，不跨状态缓存速率。

逐块读取对象、1-token 块、max-min 分配、带宽、启动延迟、输入、TFS、全局锁、
socket 协议和壁钟推进规则均保持。原正式实验不改写。等价性针对相同模型提交时刻；
壁钟服务中的提交间隔会随实现变快而变化，因此不要求实际并发和完成时间保持。

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python tests/test_storage_optimization.py --output results/equivalence_new
.venv/bin/python scripts/compare_storage_diagnostics.py \
  results/storage_step2_before_20261024 results/storage_step2_after_20261024 \
  results/comparison_new
```

输出目录必须不存在。等价测试加载第一步已提交的冻结旧源码，校验其 SHA256，
覆盖96场景、7,828个逐事件状态；服务统计和完成反馈另用固定时钟对拍。
对比脚本校验两组冻结源码、原始事件摘要、输入配置及独立虚拟时间控制，
不要求旧结果源码等于当前已优化源码。旧结果如需重算完整分析，须使用其源码快照。

同机先运行旧版27次，再运行新版27次；两组各216次合成读取，测量期间未并行跑测试。
结果在 `results/storage_step2_{before,after,equivalence,comparison}_20261024/`。
新服务RPC提交处理中位数1.373→0.677ms，历史工况5.613→0.626ms。
未插桩RPC的三次读取峰值由1/1/1变成2/2/1，仍未消除壁钟与实现开销的耦合。
108项全量测试通过；没有重跑完整360条实验，也不将本机耗时当作生产延迟。

## 存储并发第三步：带块数权重的合并记账（2026-10-24）

存储服务的 `max_min_fair` 默认采用 `sim/grouped_storage.py` 的 `GroupedStorage`。
同一次提交在同一盘上的块，用一个物理对象记录；块数作为带宽分配权重。
每块剩余字节、需求和完成阈值仍按原值计算。不同提交、不同盘不合并。
1-token块、逐块命中核对、多盘HRW放置、读取字节和带宽公式保持。
这只替换存储服务的记账表示；DES的 `Storage` 与 `active_split` FIFO保持原实现。

在storage配置中设置 `max_min_read_representation: "per_block"` 可回退逐块服务；
`"grouped"` 为max-min服务默认值，非法值直接报错。配置文件不必修改。
`pending`、`queued_reads`、`active_paths`仍计逻辑块，不能用物理对象数解释带宽竞争。
`rates()`及snapshot的逐路径映射在显式观测时展开，模型内部推进不展开。
JSON/hex及命中校验仍逐块处理，完整快照仍可能产生随块数增长的观测开销。

聚合改变浮点求和顺序，因此采用明确数值预算对拍，不再声明逐位相同。
同时修复合并模型的零进展边界：若正的剩余传输时间加到当前时刻后被舍入为当前
时刻，则使用下一个可表示时刻；剩余字节仍完整计入传输。原逐块实现也有此边界，
但本轮不修改DES/逐块回退实现。此处理不是统一时间缩放，不能替代第四步。

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python tests/test_storage_grouped.py --output results/grouped_equivalence_new
# 同一诊断入口会使用当前Service的默认表示；输出目录须不存在。
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/diagnose_storage_submit.py --output results/grouped_timing_new
```

本机52场景、3,976检查点及独立权重/反馈测试通过。当前512块单盘请求只创建1个
读取对象，但仍按512块分配带宽。同机前后各27次试验：RPC提交处理0.612→0.088ms，
未插桩RPC客户端调用4.217→1.151ms，读取峰值2/2/2→7/7/7；直接调用达到8/8/8。
并发增加后读取竞争显现，RPC读取中位数0.569→2.611ms，并非修改了读取量或带宽。
证据见 `results/storage_step3_{before,after,equivalence,comparison}_20261024/` 和VALIDATION。
全量114项测试通过；本轮没有重跑完整360条实验，后续仍需时间缩放稳定性检查。

## 进程栈统一时间缩放（2026-10-24）

顶层配置新增 `wall_time_scale`，默认1，必须为有限且不小于1的数。
K表示每个模型秒对应K个真实秒。入口 `run_tfs_stack.py --time-scale K` 可以覆盖配置；
该入口将同一配置传给存储、gateway、Executor和回放器，避免各层分别设置。

```bash
.venv/bin/python scripts/run_tfs_stack.py --config configs/mooncake_first_seen.json \
  --trace /path/to/small-trace.jsonl --output results/scaled_small \
  --mode immediate --workers 2 --time-scale 10
```

输入arrival_s、计算公式、带宽B/s、KV字节、画像、期限、SLO、反馈延迟和老化配置
继续采用模型单位。存储和gateway用 `(monotonic-origin)/K` 推进模型时间；
Executor实际等待 `K*计算耗时`，回放按 `K*arrival_s` 发起请求。
引擎事实事件保留真实monotonic时间，在gateway减去其真实起点后除K，随后再加
模型反馈延迟。模型期限因此自动对应K倍真实期限，不修改TFS或带宽公式。

HTTP结果中的ttft_s/e2e_s、actual_arrival_s和model_elapsed_s为模型秒；
ttft_wall_s/e2e_wall_s、wall_clock_s和步骤sleep_elapsed_s保留真实秒。
步骤日志另记录sleep_requested_wall_s、sleep_elapsed_model_s及wall_time_scale。
完整实验校验会核对storage/gateway/replay的K，不能将真实窗口直接与模型带宽比较。
独立运行replay_http.py时需显式传入相同的 `--time-scale`；不建议手动分层启动缩放实验。

实际Python处理、HTTP/Unix socket、轮询和原生Dynamo/vLLM控制代码没有乘K。
它们的归一化开销随K减小，正是本轮敏感性检查的对象。gateway的策略唤醒时刻按
模型时间换算，0.25s心跳、0.001s最小轮询和断连轮询仍是实际实现间隔。
HTTP容错超时随K放宽；启动、停栈及socket元数据操作超时保持运维单位。
这不是统一离散事件时钟，也不证明目标生产系统的控制面延迟应为零。

`scripts/validate_time_scaling.py` 可冻结小负载与源码，运行存储隔离和真实完整栈
两类缩放试验，并保留原始数据、每次峰值、模型/真实时间与局部稳定性检查。
输入配置和trace须明确指定，输出目录必须不存在。该脚本默认1/10/50/100倍、各3次；
完整栈使用真实Dynamo round-robin和指定数量的真实vLLM/EngineCore。
它不是360条正式重跑，也未验证所有基于壁钟预测的路由策略都兼容时间缩放。
K会增加真实运行时间，不能把小负载得到的K直接视为正式实验的通用选择。


## 原360条场景并发验收（2026-10-24）

`scripts/validate_original_concurrency.py` 使用归档的同一360条输入、8个真实引擎、
120 GB/s共享池、GLM5计算公式及first_seen命中绑定。启动适配沿用原实验的预分词、
画像缓存、绑定源引用、512连接池与1024连接等待队列；600秒HTTP超时随K缩放。
配置、输入、适配器及源码摘要逐轮冻结；独立复核入口为
`scripts/audit_original_concurrency.py`。实际运行证据与失败预试分别保留在
`results/concurrency_original_step5*_20261024/`，结果以VALIDATION末节为准。

本轮修复了`run_tfs_stack.py`的启动检查。仅出现模型名称或日志中的Registered不足以
证明8实例可路由。入口先启动全部引擎，等每个引擎明确注册generate端点后，再启动前端读取完整注册
快照。入口等待原生前端`/v1/models/sim-opt/ready`报告dynamo命名空间的aggregated
实例数达到预期，才开始测量回放。registration.json、readiness.latest.json与
readiness.json保存注册及前端事实；这些等待不计入TTFT，不修改真实Dynamo路由或vLLM调度策略。

复跑时在新结果目录复制上述归档的config.json、trace.jsonl、gateway.py、
storage_transport.py，然后执行（目录名必须为results的直接子目录）：

```bash
PYTHONHASHSEED=0 .venv/bin/python scripts/validate_original_concurrency.py \
  --output results/<新验收目录> --name full_immediate_k1_r0 \
  --mode immediate --scale 1 --wall-budget 1200
.venv/bin/python scripts/audit_original_concurrency.py results/<新验收目录>
```

--prefix仅用于预试；其结果不能冒充360条验收。存储峰值恢复、时间倍率稳定性、
TFS策略收益是三项不同结论，不能仅凭峰值大于1认定性能估计已准确。

原始大文件由外层私有工作区`tooling/artifacts.py`归档；恢复完整工作区后，原路径可读。
只克隆DV分支时不会自动拥有这些原始文件；需同时恢复工作区artifacts。
本入口要求固定Dynamo版本提供原生模型readiness接口，不会在接口不可用时降级为日志检查。


## 真实多进程栈的逐层读算（2026-10-24）

新增 `kv_transfer_mode`。`bulk` 保留原来的整段加载后计算；`layer_serial` 用于逐层串行对照；
`layer_pipeline` 在本层计算开始时预取下一层。配置缺省仍解释为 `bulk`，历史配置不改写。
`run_stack_experiment.sh --admission ...` 的缺省配置改为
`configs/mooncake_first_seen_layerwise.json`，采用 78 层重叠模式。原整段配置仍可显式选择。

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
.venv/bin/python scripts/run_tfs_stack.py \
  --config configs/mooncake_first_seen_layerwise.json \
  --trace configs/mooncake_first_seen.jsonl --output results/layer_pipeline_run \
  --mode tfs --workers 2 --kv-mode layer_pipeline

# 独立功能矩阵：8条合成请求、2实例、64-token预算、三种加载模式及两种准入。
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
.venv/bin/python scripts/validate_layerwise_stack.py --output results/layerwise_validation
```

逐层模式的连接器向真实调度器报告“有外部命中，当前调用不是异步预加载”。调度器按原生规则
形成 `SchedulerOutput`，分配逻辑 KV 块并决定各请求本步的 token。`SimExecutor` 固定这个 batch，
先读第 0 层；每层等本批所有远端前缀读完，再计算，并提交下一层读取。下一层同时等待
“上一层计算结束”和“本层读取结束”。最后一层计算结束后，结果仍经真实 `update_from_output` 返回。
零读成员也等待同批屏障；执行器不会为慢成员临时重组 batch。
bulk 的异步整段预加载与逐层模式的同步执行会改变请求进入 batch 的时机，
因此跨模式的实际 batch 和总 padding 计算量不保证相同，TTFT 差不能直接作为纯重叠收益。

KV 每 token 总量仍为 109824B，逐层为 1408B×78。层读取会完整消耗浮点剩余量，再报告完成；不会沿用每块 1e-5B 的提前完成阈值。
原 bulk 阈值保持不变；舍入到同一时刻时仍只前进一个可表示时钟刻度。
层读取复用原盘放置、块数权重、
max-min 分配及共享链路约束；支持统一池和三盘。需求仍取原请求 `V/T` 并平分到逻辑块，
没有引入 SA 的组批或带宽策略。三层 dense 和七十五层 MoE 分别使用原 GLM 公式耗时，
引擎固定开销仅计入第 0 层，总计算公式不变。`layerwise.startup=once` 仅第 0 层收取原启动延迟，
用于保留整段模式的启动成本；`per_layer` 则每层收费。这是模型选项，不是硬件测量结论。

外部前缀只加载一次。后续 chunked prefill 的 h 含已驻留的外部前缀和新计算的本地前缀，
不会把增加的 h 当成新的远端读取。一次请求加载只有一次 `load_submitted/load_completed`；
`layer_load_submitted` 分层累计实际提交字节。`load_done` 在全部层读取完成时释放；
默认 `first_token` 仍在首 token 反馈后释放。TFS 阈值、期限、配额与释放策略没有改动。
请求画像仍沿用原参考服务时间，未按逐层重叠重新标定 SLO 或配额。

`storage.final.json` 新增 `layer_loads/layer_read_history/layer_spec/t0_monotonic_s`。
worker 日志包含逐层读提交、就绪观察、计算开始/结束。`layer_check.json` 独立复核字节、
盘放置、逐层因果、固定 batch、计算公式和预取重叠；`hit_check.json` 检查实际 h/u 连续。
`timing.duration_s` 始终仅表示模型计算时间。逐层模式的 `execution_elapsed_wall_s`
（兼容字段 `sleep_elapsed_s`）包含读取等待、计算及真实 RPC/日志开销，不能当作纯计算时间。
模型时间仍按统一 K 换算。0.5ms 就绪轮询属于真实实现开销，不是额外的模型读取延迟。

边界：当前只承载只读远端前缀、prefill-only、单输出 token、关闭本地前缀缓存及 grouped
max-min 存储。抢占后重复加载明确报错；未实现取消回收、写回或容量淘汰。实际 GPU 张量和
ModelRunner 层函数仍被替换，层次由 CPU 执行器模拟。小负载矩阵用于功能验收，不能证明
TFS 收益或目标硬件性能。正式运行记录见 VALIDATION 的逐层读算章节。


## SA 八层 A/B/C 的真实多进程对照（2026-10-24）

新配置 `configs/sa_abc_stack.json` 仅用于 SA E26f 口径实验。计算模式为
`sa_e26f`，请求画像为 `sa_reference_v2`，读取模式为 `layer_pipeline`。
它不替换 Mooncake/GLM 缺省配置，不改变固定上游源码。

- 计算复用 `sim/sa_model.py` 的 SA 八层公式。实际 SchedulerOutput 决定
  本步 `(h,u)`。只累加真实新计算 token，不按最长请求补齐，也不增加 `prefill_time`。
- 存储复用原 `LayerStorage` 聚合 max-min。每个实际 batch 的每层只有一个流。
  首层申请 q_max；下一层申请 min(q_max, V_batch_layer/c_current_layer)。
  预取截止时刻仍未读完时提升到 q_max。不是 grouped 按块份额模型，也不是三盘模型。
- 前缀合同仍按真实 block hash 校验；同一步共享读取完成屏障；后续 chunk 不重读前缀。
- SA 准入复用 `SAAdmission`。P75=0 时不限制配额，其他情况沿用原规则。
  保守占用从准入保留到首 token；不使用实测剩余进度缩减画像。

示例（包目录执行；已有输出目录不能覆盖）：

```bash
.venv/bin/python scripts/prepare_sa_abc_stack.py --output results/<新实验目录>
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/run_sa_abc_stack.py --output results/<新实验目录> \
  --name immediate --mode immediate --scale 10 --workers 8
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/run_sa_abc_stack.py --output results/<新实验目录> \
  --name tfs --mode tfs --scale 10 --workers 8
```

准备器从归档的 `D1fA-s416.json` 取 A/B/C 各120条。总读取205581189120B，
到达样本整体缩放为2.14147072模型秒，对应120GB/s×80%的有限窗口输入需求。
不把实际读取利用率强制设为80%，不调整计算负载。A保留16条簇到达，B/C保留同源
随机样本的相对间隔。有限样本归一化不等于重新生成无条件泊松过程。
文本采用3个安全模板，逐请求固定h避免共享模板扩大远端命中；本地prefix cache关闭。

本配置8个真实vLLM/EngineCore实例、max_num_seqs=4、token预算8192、输出1个token。
num_blocks=196114是逻辑KV容量；没有模拟真实模型权重/HBM分配。与旧SA的32设备、
max_num_seqs=8不相同。K=10统一拉长到达/读取/计算再折回模型秒，不代表完成了时间收敛。
实验复用已归档的分词、画像、AF_UNIX backlog及HTTP连接池适配器，两组相同。
原生Dynamo前端必须确认8实例就绪才回放。脚本记录来源、资源、失败与清理状态。

`--smoke --workers 2`每类选择2条，只做功能预试。正式360条验收包括独立Fraction
公式复算、实际h/u、总字节、层屏障/预取顺序、SA流需求及带宽积分。
`tests/test_sa_stack.py`覆盖聚合流解析例、到期提升和画像。全部原始事件留在results，
外层工作区归档保存；不要将小样本、单轮或壁钟控制开销解释为生产性能。

本次大记录预试还发现storage客户端接收实现反复拼接、搜索完整前缀，导致全量
stats导出耗时随响应长度呈平方增长。`StorageClient._call`改为各分片仅搜索一次、
最后合并一次，保留JSON-lines、多响应残留、异常语义。该修复不修改服务端读取
分配；正式两组共用修复版本。`test_storage_client_framing.py`验证真实socket
分片/合并响应、远端错误与截断。导出发生在首token测量之后，不能计入TTFT。

SA waterfill在Σq≤B时按q分配后均分剩余容量，实际速率可高于申请率。
q仅在争抢分配中限制低申请流，不是独占时的硬上限。报告须明确此继承规则，
不能套用grouped逐块分配的解释。

## 单进程 SA ABC 的轮询对照（2026-10-25）

`sim.sa_aligned` 支持顶层 `router_mode="round-robin"`。缺省仍为 `kv`，
保留既有原生 `select_and_reserve` 负载选择。轮询适配器按准入放行次序循环选择
0..N−1，通过原生 Dynamo `create_reservation` 显式登记目标实例；首 token 与
完成分别调用原生 `prefill_complete`、`free_reservation`。显式设置完整 prompt 的
prefill hint，保持旧 DES 记账口径；轮询本身不读取这个负载决定目标。

此入口仍在一个 Python 进程中创建 N 个真实 vLLM Scheduler/KV 管理器，使用
离散事件虚拟时钟。Python 循环选择不是 Rust HTTP frontend，不应称为运行了
完整 Dynamo/EngineCore 进程栈。实例内组批、SA 八层读算、配额释放规则不改。

从冻结的多进程准备目录转换相同 h/u、计划到达、T0、期限和资源数值：

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/run_sa_abc_des.py \
  --source results/sa_abc80_20261024 --output results/<新的目录>
```

脚本拒绝覆盖，冻结输入/配置/运行源码哈希，顺序运行 immediate/TFS，各自执行
`check_sa_results.py`；该检查新增逐请求轮询次序断言。保留旧 DES 合成 token IDs，
本地缓存关闭。与真实进程栈共享计划到达和形状，并不复刻 HTTP 实际到达偏差、
原生前端实例池排列、真实通信时间或同刻请求处理方式。结果不能作为仅改变进程
数量的严格因果消融。`tests/test_router.py` 用真实 Dynamo 服务验证不等负载时
仍循环分配，以及首 token 前后和最终释放的记账守恒。

正式记录 `results/sa_abc80_des_rr_20261025/`；用户暂停前的旧负载选择对照保存于
`results/sa_abc80_des_20261025/`，不混入轮询主表。实际数值与检查见 VALIDATION。

## 四种子有限窗口压力对照（2026-10-25）

`run_sa_pressure.py` 使用冻结的 E26f C_A 416/417/418、C_B 424，保持 h/u、
16条A簇及B/C相对到达间隔。按总读取字节/(带宽×rho)求本次计划窗口，整体缩放
原到达时刻，按新带宽重算T0和arrival+4T0。默认单进程12实例、60GB/s、rho=.8、
max_num_seqs=4、token预算8192、原q_max=200GB/s，轮询选择与原生Dynamo记账。

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/run_sa_pressure.py \
  --output results/<新的目录> --workers 12 --bandwidth-gbs 60 --rho .8 \
  --max-num-seqs 4 --seeds 417 416 418 424
```

每份360请求，immediate/TFS各一次。输出拒绝覆盖，冻结源输入、转换输入、配置、
运行源码、SHA与日志；运行后验收真实轮询、读算因果、字节、内存及原生记账回收。
可用`--prepare-only`仅准备；`--q-max-gbs`等参数必须在报告显式记录，不能与容量混淆。
旧`check_sa_results.py`的参考时间验收原来写死120GB/s/200GB/s，本次改用当前配置。
`tests/test_sa_pressure.py`用独立Fraction公式检查60GB/s、qmax限速及旧参考值拒绝。

80%仅表示这次有限样本的输入平均，不代表长期泊松生成器均值。C_A计算输入约75.89%，
C_B/424为118.05%，不能把424当作75.9%的同一工况。TFS阈值是动态P75，不是固定两个A。
应单独统计到达阶段与排空阶段；完整读取未结束包括未来层，不能直接当作存储排队。
实际结果见VALIDATION；不运行完整frontend/EngineCore，不新增生产性能声明。

### 2026-10-25：统一3.612秒窗口的12实例压力对照

在上一轮相同12实例/60GB/s与原E26f输入上，仅将到达窗口改为3.612秒。
使用`run_sa_pressure.py --rho 0.9486027552602437`，其余参数与上节相同。
该rho=205581189120/60e9/3.612，对本轮四份相同总读取量输入有效；不是通用窗口参数。
存储输入94.8603%，C_A计算89.9912%，C_B计算139.9804%。原始记录
`results/sa_pressure12_60_w3612_20261025/`，8组均通过验收；数值与限制见VALIDATION。
没有修改运行算法或上游代码。复跑必须使用新的输出目录，避免覆盖冻结数据。

### 2026-10-25：12实例原生进程栈同输入初验

原始驱动`results/sa_stack12_w3612_20261025/`与四个同层
`sa_stack12_w3612_s<seed>_20261025/`保留完整360条输入和两方案原生栈记录。
这是同一3.612秒输入、12实例/60GB/s与K=10壁钟运行，全部8组通过功能验收。
复用原入口的冻结副本，只调整脚本位置和资源断言；运行算法不变。原生readiness
确认12实例后才回放，每组结束清理；完整数值、计时口径和限制见VALIDATION。
417总体均值差约+0.7%/-2.0%，其他组最大约12.2%；不能作为全数值等价或K收敛证明。

2026-10-25 零控制耗时离线对照：`results/zero_control_cost_sa12_20261025/`复跑四种子两臂，
保留原生实际到达与每请求worker，在DES中重新调度；8组2880请求通过，事件轨迹与
前次同条件逐位一致。它是虚拟时钟反事实，不是原生栈只关闭控制开销的严格消融。
结果及限制见VALIDATION对应条目，外层`note/tfs_dv/零控制耗时离线重放-20261025/`。

2026-10-25 路由受控对照：`results/zero_control_cost_sa12_routing_20261025/`保持12实例/
60GB/s/3.612秒与组批参数不变，仅对比round-robin和原kv在线负载选择。32组11520
请求通过，轮询8组事件与旧值逐位一致，运行时代码未改。负载选择3次中位数中，
416/418/424的TFS均值与完成时间均转优，417均值基本持平且仍拖尾。详见VALIDATION；
外层`note/tfs_dv/十二实例负载选择对照-20261025/`含报告与复跑/绘图脚本。仅DES，
不构成原生12进程验收，也不证明3次统计收敛。

## 原生SA进程栈启用Dynamo负载选择（2026-10-25）

`run_sa_abc_stack.py`现在默认`--router-mode kv`。真实前端按负载选实例，关闭缓存
重用得分；显式`--router-mode round-robin`可复跑轮询。可从既有冻结准备目录复制
config.json/trace.jsonl，拒绝覆盖新的输出目录。入口继承源实例数、存储容量和输入，
不再写死8实例/120GB/s。完整实验的360请求/字节/SA模型检查保留。

```bash
PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/run_sa_abc_stack.py \
  --source results/sa_stack12_w3612_s417_20261025 \
  --output results/<新的目录> --name immediate --mode immediate \
  --scale 10 --workers 12 --router-mode kv
# 同一目录第二臂不再传--source；使用--name tfs --mode tfs，其余参数相同。
```

通用`run_tfs_stack.py`新增`--router-mode kv|round-robin`，未传时继承配置；旧配置
缺失router_mode仍用round-robin。默认SA配置已显式kv。其余脚本/旧结果不自动改写。

kv输出`routing.launch.json`、`routing.metrics.jsonl`、`routing.final.json`及
`routing.check.json`。新增检查要求每次原生选择均为候选最低分、确有不等负载
选择，观察到非零负载且最终记账清零；所有旧功能检查照常执行。评分同分仍随机，
日志和每秒采样属于本次诊断开销。运行时不会以Python选择器替换Dynamo路由。
本轮验收结果和原始记录见VALIDATION日期对应条目。

冻结SA入口的`--output`须直接位于包内results/下一级，例如results/sa12_kv_417。
历史网关适配脚本按该目录深度定位本地模型；入口现在会在启动前拒绝不支持的路径，
避免把本地模型路径误判为远程仓库。此约束不影响run_tfs_stack的一般输出路径。

2026-10-25 补跑原生kv的416/418/424两臂：原始矩阵为
`results/zzz_stack_kv_matrix_20261025/`，每种子记录在对应
`zzz_stack_kv_s<seed>_20261025/`。6组2160请求均通过；未改运行时代码/输入/资源。
417复用前次两组。四种子汇总、验证与单次运行限制见VALIDATION；外层报告位于
`note/tfs_dv/十二进程负载选择四种子-20261025/`。不要自动重复已通过矩阵。


2026-10-26 完成同配置两次原生补跑：矩阵为
`results/zzzz_stack_kv_repeats_20261025/`，新增16组5760请求全部通过；
每种子/方案累计3次。逐格目录为`zzzz_stack_kv_r<1|2>_s<seed>_20261025/`。
未修改运行时代码；配置和输入摘要与上一轮一致。结果及统计口径见VALIDATION，
外层`note/tfs_dv/十二进程负载选择三次重复-20261025/`含完整对照；不要重复执行已完成矩阵。

2026-10-26 新增默认关闭的细粒度诊断：设置`SIM_FINE_TRACE_DIR`后，Worker包装入口
启用`sim/stack/fine_trace.py`，对固定vLLM方法做运行期计时。固定上游文件不改。
探针使用monotonic_ns，进程内缓冲；回放结束后仅对已登记的探针进程发送SIGUSR1刷新。
`scripts/run_seed416_fine.py`运行本次冻结ABC，`check_fine_trace.py`验证覆盖及顺序，
`analyze_seed416_fine.py`生成互斥请求账本和嵌套RPC表。不能把嵌套RPC重复加入计算窗口。
本次在WSL 24GiB完成seed416原生/单进程DES各immediate/TFS一次，均12逻辑实例。
正式结果见VALIDATION及外层`note/tfs_dv/seed416细粒度耗时对照-20261026/`。

## 调度前重读准入 V1（2026-10-26）

完整说明：[重读信息下发与调度前准入](../docs/重读信息下发与调度前准入V1-20261026.md)。
`run_tfs_stack.py --admission-placement scheduler` 启用：网关不预占额度，
vLLM 在 waiting 路径的 `allocate_slots()` 前为重读请求申请额度。
轻读跳过申请；本轮首次 full 后跳过后续重读并继续轻读。仍在首 token 返回后释放。
默认关闭，无等待优先级和防饥饿。源码哈希保护的运行期补丁会改变调度行为。

在 Linux 包目录内顺序运行以下功能检查，输出目录须不存在：

```bash
.venv/bin/python scripts/validate_read_admission.py --output results/read_gate_layer --kv-mode layer_pipeline
.venv/bin/python scripts/validate_read_admission.py --output results/read_gate_bulk --kv-mode bulk
.venv/bin/python scripts/check_read_admission.py results/read_gate_layer/run
```

配置需要 tfs/arrival/first_token/aging off，CLI 启用参数会设置 arrival 和关闭 aging。
合成验收使用两实例、12 请求、120 GB/s 池、K10、小并发额度；不是收益实验。
对照收益时两臂应使用相同的冻结分类口径。单元及完整链路结果见 VALIDATION。
Linux CPU 环境需要与 torch 配套的 torchvision CPU wheel，例如
`pip install --no-deps --index-url https://download.pytorch.org/whl/cpu torchvision==0.26.0+cpu`；
Dynamo components 按 bootstrap_stack.sh 从固定源码安装，不使用 vllm 版本升级。

## 准入 TCP JSON-lines 对照（2026-10-27 首版）

本节记录首版 TCP JSON-lines 实现。当前默认已改为下一节的 Dynamo Runtime RPC。
显式选择 `tcp` 时，申请/回滚/加载反馈均为 TCP 长连接上的 JSON-lines RPC。
runner 自动分配回环 TCP 端口，地址写入结果目录的 `admission.rpc.json`。
`run_tfs_stack.py --admission-transport tcp` 可显式指定；
`--admission-transport unix` 仅用于旧传输对照。
网络超时不会自动回退 Unix 或重复申请，仍按不确定结果失败关闭。

单独启动服务时使用 `--feedback-endpoint tcp://IP:端口`；
worker 使用 `SIM_STACK_ADMISSION_ENDPOINT=tcp://服务IP:端口`。
默认超时 5 秒，配置键为 `admission.rpc_timeout_wall_s`。
详细命令、跨机时间口径和边界见上面的 V1 说明第 11 节。
完整栈验收仍是本机多进程；TCP 回环往返时间不能当成跨机实测延迟。

## 原生 Dynamo Runtime 准入 RPC（2026-10-27）

当前多进程入口的 `admission.rpc_transport` 默认是 `dynamo`。
`run_tfs_stack.py --admission-transport dynamo` 使用固定版本 Dynamo Runtime 的
TCP 请求面和 MessagePack 编码。申请、回滚与加载反馈直接发到账本端点，
不经过推理路由器，也不经过外部 worker 转发。
账本仍在原独立准入服务进程；HTTP 登记/转发、首 token 释放和扣账共用原状态。

EngineCore 初始化后创建常驻通信线程、事件循环与 Runtime 客户端。
`schedule()` 等待该线程的 RPC 结果；这仍会阻塞本轮调度，包括后续轻读。
没有在调度热路径重新创建 Runtime、发现服务或建立客户端。
独立审计的申请壁钟 RTT 包含通信线程交接、Runtime 与账本处理，不除以 K。

runner 为每轮运行生成独立的 `dyn://命名空间.ledger.control` 端点，并在
`admission.rpc.json` 保存实例 ID、账本启动标识、进程 ID 和协议。
EngineCore 固定目标实例，核对启动标识；超时、错误、发现变化时失败关闭，
不自动改投其他账本或重放不确定申请。该账本仍只允许一个实例，无跨机容错。

功能验收：`.venv/bin/python scripts/validate_read_admission.py --output results/runtime_layer`。
增加 `--kv-mode bulk` 验证整次加载；`--transport tcp` 或 `unix` 可复跑旧通信对照。
完整手动部署参数及发现配置见 V1 说明第 12 节。整套仿真仍在同机运行，
不能把本机 TCP/IP 往返解释为物理跨机延迟。实测以 VALIDATION 为准。

## 2026-10-28：priority 统一为 EDF，计算时间按模型实现

`policy=priority` 现在统一使用 EDF（最早截止时间优先）。显式配置为
`"policy": "priority", "request_ordering": "edf"`；FCFS 对照仍可使用。
原生进程栈可传 `--request-ordering edf`，单进程 `sim.run` 可传 `--policy priority`。
排序键为冻结的 `deadline_s`，量化成整数模型纳秒。期限相同的请求沿用原生比较规则。
期限仍由 `arrival_s + slo_multiplier * T0` 生成，倍率默认 4、可配置。
排序不减 now 或 T0，过期不丢弃请求；原生 priority 也可能影响抢占。

`sim/stack/edf_priority.py` 经 annotations → SamplingParams.extra_args → EngineCore
传递画像，使用真实 `PriorityRequestQueue`。`scripts/check_edf_priority.py <结果目录>`
检查传递内容、固定排序键和原生堆顶，需要启用 fine_trace 的运行记录。
旧 `budget_priority` 配置名称会警告并转成 EDF；旧 Python 入口仅转发到新实现。
2026-10-27 的剩余预算实验只能使用其冻结运行包和旧审计器复核，历史结果没有改写。

`sim/compute_time.py:get_compute_time_model` 返回 `ComputeTimeModel` 实现，
统一入口为 `estimate_step(shapes, decode_tokens)`。ABC 使用 `SAComputeTimeModel`：
实际 token、attention 配对和批级效率；Mooncake 使用 `GLM5ComputeTimeModel`：
原 GLM padding、参考 DP/EP 与 dense/MoE 逐层公式。线性实现独立保留。
本次封装接口不修改计算公式、硬件布局或 decode 支持范围。

`sim/reference_time.py:get_reference_time_estimator` 为不同模型选择参考估时实现。
参考 T0 按实际 bulk、layer_serial 或 layer_pipeline 合成读算时间。
pipeline 使用 `x1 + sum(max(x_l,c_(l-1))) + c_L`，后续 chunk 只计算，不重复读前缀。
ABC 保留 SA 带宽上限；Mooncake 保留 GLM 逐层差异。零读请求只有计算时间。
参考画像版本为 `*_reference_v2`，旧 v1 配置会警告并迁移。Mooncake pipeline 的
T0 和 SLO 期限因此会变化，不能把新旧结果的差异全部归因于 EDF。
设计与证据边界见 `../docs/EDF统一排序与分模型估时接口-20261028.md`。


### 等待折扣评分支持 block16（2026-10-27）

`routing_accounting` 可选启用时支持 block_size=1/16。账本保持 token 单位，
原生 scorer 折扣按 `(1-α) * waiting_tokens / block_size` 换算为 block，不取整。
资源预留与 KV 分配仍完整计数；block1 行为不变。修改 Rust overlay 后须重建，
启动器继续核对源码与 native SHA。macOS 构建产物 lib_core.dylib 会复制为
隔离目录中的 dynamo/_core.abi3.so，未覆盖安装环境。
完整说明见 `../docs/等待重请求折扣计分与获准补齐-20261027.md`。

### 空闲实例强制准入（2026-10-28，默认关闭）

`admission.idle_bypass=true` / `run_tfs_stack.py --idle-bypass`：当实例没有 running、没有未释放的强制请求，且两个等待队列只有未获准、未计算、非阻塞的重读时，选择原生队列头一条。先完成原生 KV/slot 分配，再异步通知中央强制记账；不等待同步额度回复，允许超过全局 cap。普通准入仍计入全部额度，首 token 才释放；每实例最多一条强制配额。

强制请求的通知和读取反馈通过有序后台连接发送。网关完成事件可暂存等待先前事实，但保留真实时间。默认关闭时保持原同步语义。该功能只支持 scheduler/arrival/first_token、逐层读算、零反馈延迟、Unix或经验证共享单调时钟的原生/TCP通道。它不改计算、存储、组批上限或原生 picker，也不保证消除存在轻请求时的全部空泡。

实现与边界：[TFS空闲实例强制准入](../docs/TFS空闲实例强制准入-20261028.md)。测试 `tests/test_idle_bypass.py`，审计 `scripts/check_read_admission.py`。实验见 `VALIDATION.md` 的实际运行记录。

### Mooncake360 / block16 四方案实测（2026-10-28）

复用conversation前360条、4秒到达、每条96%人工远端命中后向下对齐16-token块。
12真实vLLM实例、K=1、4序列、8192新token/步、120GB/s共享池、GLM78层逐层预取。
四组同源码同原生binary，每组72条预热排空并重置分类历史，再计时正式360条。
直接准入/旧网关TFS/新scheduler＋预算排序＋α0.1/新TFS＋空闲强制准入，
完成9493.256/10833.434/8603.960/7461.240ms，SLO28.611/25.556/30.000/24.722%。
各正式360/360、全量432检查通过；代码未改，实验驱动及实际证据保存在
`results/mooncake360_block16_four_evidence_20261028/`。
单次K1运行包含314–708ms共享存储服务主机处理暂停；不证明跨重复稳定收益，
也不能单独与旧WSL/K10/block1结果比较块大小收益。完整数据与限制见VALIDATION末节。


### 存储服务 GC 测量窗口（2026-10-28）

`run_tfs_stack.py` 缺省使用 `storage.gc_policy=defer_during_run`：起栈就绪后先全代回收，再暂停存储进程的自动循环 GC；请求与读取排空后恢复原 GC 状态并回收。引用计数释放保留。计时段含预热的自定义驱动应在预热排空后通过 `StorageClient.gc_end(collect=False)` / `gc_begin()` 重新划定正式窗口。原存储服务独立入口仍缺省自动 GC，需要编排端明确控制窗口。

使用 `--storage-gc-policy automatic` 或配置 `storage.gc_policy=automatic` 可保留原行为。两者不改输入、存储带宽或计算公式。`storage.gc_max_rss_bytes` 缺省 4 GiB，`gc_max_window_s` 缺省 600 壁钟秒；每 0.25 秒采样存储进程内存。超限或监控失败将恢复 GC、记录失败并拒绝后续数据 RPC，不能作为成功实验。Linux 使用当前 RSS，macOS 使用峰值 RSS。长时间或大规模运行应显式设置上限。

输出 `storage.gc.begin.json`、`storage.gc.end.json`，异常时另有 `storage.gc.cleanup.json`。存储最终快照包含 `gc_window`（边界、采样数、内存峰值、恢复状态及失败原因）；常规验收检查窗口结束、恢复和无保护失败。新增安全边界测试为 `tests/test_storage_gc.py`；实际复跑结果见 `VALIDATION.md`。此控制只避免存储仿真进程的循环 GC 混入计时，不代表消除了所有宿主开销或证明调度策略收益。
# 2026-10-28：实际组批 prefill_time 已接入

`compute.mode=glm5_d950pr` 现在使用 `glm5_actual_batch_work_v2`。
正式模块为 `sim/timing_models/prefill_time_batch.py`，不依赖外层工作区工具。
线性工作量按实际新增token求和，indexer保留逐请求长度，MoE与dispatch/combine
共用守恒的路由工作量。原冻结算子文件和SHA不变，原生组批和存储分配规则不变。
详细设计见 [接入说明](../docs/prefill_time实际组批公式接入-20261028.md)。

```json
"compute": {
  "mode": "glm5_d950pr",
  "batch_time_model": "glm5_actual_batch_work_v2",
  "tp": 1, "dp": 8, "ep": 8,
  "kv_hbm_reuse": true,
  "sparse_HBM_hit_ratio": 1.0
}
```

TP/DP/EP是固定的公式参考布局，默认1/8/8，EP须为DP×TP；不能用它自动推定真实
worker卡数，不能随实际batch变化。GLM画像为`glm5_reference_v3`，执行时间、T0、
压力与deadline使用同一模型和布局。旧v1/v2标签会警告迁移；历史报告仍用冻结包，
不能把新旧SLO变化解释为单纯的TFS收益。SA/ABC与线性公式保持不变。

功能验收：`PYTHONHASHSEED=0 .venv/bin/python scripts/validate_prefill_batch.py --output results/<新目录>`。
验收包含bulk直接准入、pipeline调度前TFS，两真实worker和混合/分块请求。
独立审计器按事件中的模型版本复算，历史无版本事件仍按旧算法解释。
没有GLM5/D950PR实机定标；专家均匀路由、kernel效率与逻辑通信仍为模型假设。
## 2026-10-29：前置TFS与层级反馈开销对照

研究配置可用`admission.placement=gateway`、`classification=arrival`在原生Dynamo选实例前
保留重读待选队列。配额仍持有到首token；不是按实际IO开始时间扣账。
具体配置、默认边界与复现入口见
[`docs/TFS前置分诊与反馈开销对照-20261029.md`](../docs/TFS前置分诊与反馈开销对照-20261029.md)。

新增显式研究开关`experiment.batch_feedback=true`，同一batch同一层合为一个同步帧。
另可设置`coalesced_feedback=true`，在下一同步控制/生命周期事件前合并确认层事实。
后者会延迟中央可见性，不能据RPC减少直接认定更快，默认关闭。
活动许可索引、64KiB审计缓冲与流式JSONL整数复用不改变许可、输入或模型语义。
输入数组仍相互独立。新模式定向TCP与完整Unix实验的实际结果以VALIDATION为准。

相关测试为`test_batch_feedback`、`test_coalesced_feedback`、`test_trace_io`、
`test_gateway_triage`及既有许可、TCP、层屏障和准入测试。完整原始记录留本机，不推送token数组。
