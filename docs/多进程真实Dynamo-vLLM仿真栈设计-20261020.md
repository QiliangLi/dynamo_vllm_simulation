# 多进程真实 Dynamo + vLLM 仿真栈设计：真实进程 + mock 执行层 - 20261020

| 项目 | 说明 |
|---|---|
| 日期 | 2026-10-20 |
| 性质 | 设计文档（**未实现**）。给出把现有单进程 DES 实验台扩展为"本机多进程、原始 Dynamo/vLLM 代码、mock 模型执行与存储"的架构、决策、工作量与验收标准 |
| 状态 | 设计稿。标注【实测】的条目均于 2026-10-20 在本机（macOS 26.6 ARM）对固定 SHA 源码验证；标注【待验证】的留给实现阶段 |
| 相关文档 | [Dynamo_vLLM_CPU_Simulation_Design.zh-CN.md](Dynamo_vLLM_CPU_Simulation_Design.zh-CN.md)（§3.2 完整服务闭环、§18.1 SimExecutor、§21 P3——本设计是它们的实施细化，下称"总设计"）；[CPU真实代码仿真环境检视报告-20260924.md](CPU真实代码仿真环境检视报告-20260924.md) §5/§8（语义决策与路线）；[MPC调度架构前提与真实Dynamo-vLLM落地挑战-20261010.md](MPC调度架构前提与真实Dynamo-vLLM落地挑战-20261010.md)（§5.0 在线必需件——本栈是其落地验收场）；[Dynamo路由与vLLM引擎的状态交互机制-20261009.md](Dynamo路由与vLLM引擎的状态交互机制-20261009.md)（真实部署三通道）；[AB双类带宽争抢实验结果-20261008.md](../AB双类带宽争抢实验结果-20261008.md) §6（picker 随机性判读方法） |

文中源码路径相对于 `dynamo_vllm_cpu_sim/`（如 `upstream/vllm/...`）。

## 0. 一句话定位

**把原始 Dynamo 与原始 vLLM 代码按真实部署方式在本机作为多个独立进程运行**——真实 frontend（HTTP/OpenAI API + Rust tokenizer）、真实 router（选择/记账/KV 事件索引）、N 个真实 vLLM worker 进程（EngineCore/Scheduler/KVCacheManager 原码），进程间走真实 HTTP/TCP/ZMQ 协议；**只把最内层的"模型前向执行"与"远端存储 I/O"换成 mock**（SimExecutor 与 storage-sim 进程）。假的不是进程，是算力与存储。

它是对现有单进程 DES 实验台的**进程化扩展**，不是替代，也不是新的仿真内核：调度/存储/时间的仿真语义全部复用现有 `sim/` 实现。与完整真实部署相比，只差"装权重、换回真执行器"最后一步。

## 1. 三个形态对照

| 维度 | 现状：单进程 DES（已交付） | 本设计：进程化仿真栈 | 完整真实部署 |
|---|---|---|---|
| Dynamo | 进程内 `SelectionService` 对象，`upsert_worker` 固定拓扑 | frontend/router 独立进程；`file` 发现、tcp 请求面、ZMQ 事件面 | 同左 + etcd/k8s 发现，NATS 可选 |
| vLLM | 每 worker 一个 `Scheduler` 对象，仿真循环手动驱动 | 每进程 AsyncLLM → EngineCoreProc → 真实 Scheduler | 同左 |
| 模型执行 | 线性时间模型 + 合成 ModelRunnerOutput（在仿真器手里） | 同一套模型，搬进 vLLM 进程内的 SimExecutor | 真实 GPU/NPU 前向 |
| 存储 | 进程内 `Storage` 对象 | storage-sim 独立进程 + SimConnector 走 RPC | 真实共享 KV 存储 |
| 时间 | 全局虚拟时钟，确定性 | 模式 1 壁钟 / 模式 2 虚拟时钟服务 | 墙钟 |
| 可复现性 | 高（CRN、逐位复跑） | 模式 1 低；模式 2 近似确定性 | 低 |
| 时序保真 | µs 级（虚拟时间） | 模式 1 受 ms 级 IPC 抖动限制；模式 2 恢复 µs 级 | 真实 |
| 用途 | 定量对照实验（现有全部实验） | 协议/生命周期/策略生效验证；在线必需件的落地与验收 | 生产 |

两形态分工：**DES 管定量对照，进程栈管协议正确与在线路径**；策略代码（`scheduler_cls` 子类、Rust policy、时间模型）两边共用。

## 2. 进程拓扑与真实/mock 边界

```
scripts/replay_http.py ──HTTP──► python -m dynamo.frontend    真实 HTTP/OpenAI API + Rust tokenizer
（按 arrival_s 开环注入）              │ tcp 请求面（默认）
                                 python -m dynamo.router      真实路由进程；KV 事件经 ZMQ 入索引
                                       │ tcp 请求面
            ┌──────────────────────────┼──────────────────────────┐
     python -m dynamo.vllm #0    python -m dynamo.vllm #1   ...   python -m dynamo.vllm #N-1
     （带 patched/ 薄补丁）
        └ AsyncLLM ──ZMQ── EngineCoreProc（真实 EngineCore / Scheduler / KVCacheManager / BlockPool）
              │     ├─ SimExecutor（限定名注入：时间模型 + 合成输出）
              │     └─ SimConnector（真实 KVConnectorBase_V1；异步读经 RPC ↓）
              └──ipc──► sim.storage_service 进程（Storage：active_split / max_min_fair）
     [模式 2] sim.time_server 进程：虚拟时钟，登记唤醒 + 静默推进

发现：DYN_DISCOVERY_BACKEND=file（共享目录 FileStore）  事件面：ZMQ  全部本机，零外部服务
```

| 进程 | 代码来源 | 真实部分 | mock 部分 |
|---|---|---|---|
| `dynamo.frontend` | 上游原码，不改 | HTTP 服务、tokenizer | — |
| `dynamo.router` | 上游原码，不改 | 路由选择、负载记账、KV 事件索引 | — |
| `dynamo.vllm` ×N | 上游原码 + `patched/` 薄补丁 | AsyncLLM、EngineCoreProc、Scheduler、队列、KVCacheManager、请求状态机 | — |
| （worker 内）SimExecutor | 新写 `sim/executor.py` | — | 算假耗时、睡/登记、返回合成 ModelRunnerOutput（token 恒 100、ignore_eos） |
| （worker 内）SimConnector | 现有 `sim/connector.py` 改 RPC 后端 | KVConnectorBase_V1 接口、WAITING_FOR_REMOTE_KVS 状态机 | 读请求发给 storage-sim |
| `storage-sim` | 现有 `sim/storage.py` 服务化 | — | 带宽排队/分配模型（两策略原样） |
| 回放器 / 编排 | 新写 `scripts/` | HTTP 客户端 | — |

关键边界声明（沿用总设计 §4 原则）：SimExecutor 只替换 **SchedulerOutput → 执行 → ModelRunnerOutput** 这一段；不 mock `schedule()` 返回、不重写 KV allocator、不绕过 `update_from_output()`。Dynamo 与 vLLM 的源码本体零修改；`dynamo.vllm` 适配层的少量差异以 `patched/` 副本补丁承载（与 Ascend 补丁同模式）。

## 3. 设计依据：2026-10-20 本机实测事实

以下条目决定架构可行性，全部对固定 SHA 源码（vLLM `bc150f5`、Dynamo `b83b1d9`）验证：

| # | 事实 | 证据 | 对设计的影响 |
|---|---|---|---|
| F1【实测】 | `dynamo.frontend` 在当前 `.venv`（ai-dynamo-runtime 1.5.0 本机编译 wheel + vllm 0.20.2+empty）直接 import 成功 | `python -c "import dynamo.frontend.main"` → OK | frontend/router 不需重编 Rust：本机 wheel 的 feature 为 `default=["custom-policy"]` + `select-service`（`upstream/dynamo/lib/bindings/python/Cargo.toml` `[features]`），frontend/router 不在 feature 门后 |
| F2【实测】 | `dynamo.vllm` 对 vLLM 0.20.2 的 API 兼容度很高：AST 抽取其全部 vLLM 导入（排除 `multimodal_utils/` 与 tests）逐个验证——36 个模块、46 个符号 OK；仅缺 3 个符号：`vllm.exceptions.VLLMClientError`、`VLLMUnprocessableEntityError`、`vllm.utils.FlexibleArgumentParser`（0.20.2 中位置/名称不同）；缺第三方包 `watchfiles`、`torchvision`（后者仅多模态工具路径需要，上游 pytest 自身 ignore 该目录，见 `upstream/dynamo/pyproject.toml:235`）；`vllm.entrypoints.speech_to_text` 模块 0.20.2 不存在（语音 worker 路径） | 导入探测脚本输出 | **不升 vLLM 0.28**；薄补丁约半天到一天（D1）；运行期签名漂移留缓冲（风险 R1） |
| F3【实测】 | `CpuPlatform.check_and_update_config` 不会破坏 SimExecutor 注入：只把 `distributed_executor_backend == "uni"` 改写为 `"mp"`（`upstream/vllm/vllm/platforms/cpu.py:156`），限定名字符串不受影响；`worker_cls` 仅当为 `"auto"` 时改写（:158，SimExecutor 不实例化 worker，无关）；强制 `async_scheduling=False`（:136，与现 DES 同步 scheduler 模式一致）；自动设 `VLLM_WORKER_MULTIPROC_METHOD=spawn`（:214，macOS 正好需要）；唯一的 `raise` 仅针对 FP8 KV cache（:143，不涉及） | `vllm/platforms/cpu.py:111-214` | 消除"平台层改写 executor 配置"这一未知数 |
| F4【实测】 | vLLM 0.20.2 的 `Executor.get_class` 接受限定名字符串：`distributed_executor_backend` 为 str 且非保留值时走 `resolve_obj_by_qualname`（`upstream/vllm/vllm/v1/executor/abstract.py:81-86`） | 同左 | SimExecutor 经 `--distributed-executor-backend sim.executor.SimExecutor` 注入任何真实 vLLM 进程，**vLLM 零源码修改** |
| F5【实测】 | 控制面本机零外部服务：请求面默认 `tcp`（`upstream/dynamo/lib/runtime/src/config/environment_names.rs:713`）；发现后端 `DYN_DISCOVERY_BACKEND` 支持 `kubernetes/etcd/file/mem`，默认 etcd（`lib/runtime/src/distributed.rs:702`），`file` = 共享目录 `FileStore`（`lib/runtime/src/storage/kv/file.rs:44`），跨进程可用（`mem` 仅单进程内）；事件面默认 ZMQ | 同左 | 不装 etcd/NATS；多进程用 `file` 后端 |
| F6【实测】 | `ai-dynamo` 组件包（frontend/router/vllm/mocker 所在）是纯 Python：`packages=["components/src/dynamo"]`（`upstream/dynamo/pyproject.toml:165-166`），依赖 `ai-dynamo-runtime==1.5.0`（已有，:16）；已安装的 `dynamo` 是命名空间包（无 `__init__.py`），可与 runtime 共存；注意根 pyproject 的 `vllm` extra 绑定 `vllm==0.28.0`（:64），故必须 `--no-deps` 安装 | 同左 | W0 安装方式确定 |
| F7【实测】 | Dynamo 自带 mocker 组件（`python -m dynamo.mocker`，`upstream/dynamo/components/src/dynamo/mocker/`） | mocker README | W1 控制面冒烟可完全不涉及 vLLM |
| F8【实测】 | vLLM 侧关键接口位置：`EngineCore.step()`（`vllm/v1/engine/core.py:402`）、`EngineCoreProc`（:806）；Executor 需实现 `_init_executor`（abstract.py:114，@abstractmethod）、`collective_rpc`（:198，@abstractmethod）、`check_health`（:274，@abstractmethod）及 `determine_available_memory`（:146）、`get_kv_cache_specs`（:149）、`initialize_from_config`（:118）、`execute_model`（:210）、`sample_tokens`（:230）、`shutdown`（:280）——与总设计 §18.1 清单一致 | 同左 | §5.1 方法清单确定 |

【待验证】（实现阶段确认，均有缓冲）：`dynamo.vllm` 运行期的签名/字段漂移（R1）；EngineCore 在 empty device 上的完整初始化链（R2）；KV 事件 BlockPool → ZMQ → router 索引端到端联调（R3 并入 W4）；`FileStore` 根目录的配置方式细节（W0）。

## 4. 设计决策

| # | 决策 | 理由与代价 |
|---|---|---|
| D1 | **版本路线：不升 vLLM 0.28，固定 0.20.2 + 薄补丁**。`dynamo.vllm` 的少量差异以 `patched/dynamo_components/` 副本 + diff + `scripts/apply_dynamo_patch.py` 承载（哈希核对、不改 upstream 原件、doctor `--allow-edited` 记录），与 Ascend 补丁同模式 | 升 0.28 将推翻五模式基线、A/B 结果与 Ascend 0.20.2rc1 对齐；F2 表明薄补丁成本远低于升级。代价：补丁需随上游 dynamo 版本演进维护 |
| D2 | **mock 挂载点：vLLM 的 Executor 扩展点（限定名注入，F4），Dynamo 侧零修改** | 挂在 vLLM 本来就预留的接口上，调度器代码完全不知道执行器是假的；迁回真部署只需换回真 executor |
| D3 | **时间推进两模式，先壁钟后虚拟时钟**：模式 1（壁钟）先把进程栈跑通；模式 2（虚拟时钟服务）在需要确定性/时间压缩/µs 级时序时追加。两模式的 SimExecutor/storage-sim 代码相同，只是"睡多久"换成"向时钟服务登记唤醒"，不推翻 | 总设计 §3.2 否定的是 patch `time.time()`（无法控制 Rust/Tokio 时钟），登记-静默协议不在否定范围。模式 2 是研究项，风险后置 |
| D4 | **存储服务化：storage-sim 独立进程，SimConnector 改 RPC 客户端**；`active_split` / `max_min_fair` 两策略与 Path 指派语义原样；读仍为异步完成（`WAITING_FOR_REMOTE_KVS` 状态机照常真实执行） | 带宽分配需要全局视图（单真值源）；复用现有 Storage 实现 |
| D5 | **负载入口：HTTP 回放器开环注入**（按 `arrival_s` 发请求，收集 TTFT/E2E） | 与 DES 的 trace 文件同格式；开环到达与现有实验语义一致 |
| D6 | **DES 不动**：现有 `sim/run.py` 与全部实验保持原样；新代码独立成模块（`sim/executor.py` 等），时间系数/合成输出逻辑与 `engine.py` 共用同一实现来源 | 两形态长期并存、分工（§1）；避免回归风险 |

## 5. 组件设计

### 5.1 SimExecutor（`sim/executor.py`，W2）

职责：接收 EngineCore 递来的 SchedulerOutput，按线性时间模型 `T = α + β·N_prefill + γ·N_decode`（参数与 `configs/` 现有字段同名同义）计算该步耗时，在耗时到期后兑现一个合成 `ModelRunnerOutput`（token 恒 100、ignore_eos、按 max_tokens 停止——与现 DES 完全一致）。**不做**：数值计算、权重加载、设备状态。

| 方法（F8） | SimExecutor 实现 |
|---|---|
| `_init_executor`（抽象） | 初始化与 storage-sim 的 RPC 客户端、时间来源（模式 1：定时器；模式 2：time_server 客户端） |
| `check_health`（抽象） | RPC ping + 本进程事件循环存活 |
| `collective_rpc`（抽象） | 仅支持无操作/优雅报错（无多 rank 场景） |
| `determine_available_memory` | 返回由现有 `num_blocks`/block 布局配置推导的逻辑字节数（不分配张量） |
| `get_kv_cache_specs` | 沿用现有单层 `FullAttentionSpec`（与 DES 相同的合成 spec） |
| `initialize_from_config` | 接受 EngineCore 推导的 KVCacheConfig，建立逻辑块句柄 |
| `execute_model` | 核心路径。处理 `non_block=True` 协议：返回 Future，在（虚拟/壁钟）完成时刻兑现；完成时刻若伴随存储接收完成，附 `KVConnectorOutput(finished_recving=...)` 走真实 `update_from_output()` |
| `sample_tokens` | 合成采样（token 100），仅在需要路径调用 |
| `shutdown` | 关闭 RPC/定时器 |

初始化链适配（R2 所指）：EngineCore `_initialize_kv_caches` 的 profiling 走 `determine_available_memory`（配置值），模型配置经 transformers 读 `configs/model/config.json`（现有桩），不触发任何设备路径。`VLLM_TARGET_DEVICE=empty` 环境下 `vllm._C` 缺失的告警为预期（与现 DES 相同）。

macOS 注意：`CpuPlatform` 自动设 `VLLM_WORKER_MULTIPROC_METHOD=spawn`（F3），子进程经限定名导入 SimExecutor，故 `sim` 包必须可导入（`launch_stack.sh` 统一设 PYTHONPATH 或做 editable 安装）。

### 5.2 `dynamo.vllm` 薄补丁（`patched/` + `scripts/apply_dynamo_patch.py`，W3）

清单（依据 F2，最终以运行期实际报错为准）：

1. 3 个符号 shim：`VLLMClientError` / `VLLMUnprocessableEntityError`（映射到 0.20.2 `vllm.exceptions` 的最近似异常类）、`FlexibleArgumentParser`（改从 0.20.2 实际所在模块导入）——以 try/except 导入回退实现，dynamo 新版本下自动走原路径；
2. `speech_to_text` 导入守卫（该 worker 路径本栈不用）；
3. `torchvision` 依赖绕开（多模态工具懒加载；避免为不用的大包扩 lock）；`watchfiles` 补入 lock。

落地方式与 Ascend 补丁完全同模式：apply 脚本核对源文件哈希、生成 patched 副本与 diff、不改 upstream 原件；`doctor.py --allow-edited` 记录。

### 5.3 storage-sim 服务（`sim/storage_service.py` + `sim/connector_rpc.py`，W5）

- 现有 `Storage` 对象（两带宽策略、Path 发放时指派、动态链路）整体搬入独立进程，监听 Unix domain socket / ZMQ ipc；
- `SimConnector` 保持真实 `KVConnectorBase_V1` 语义（命中查询从"本地已匹配前缀之后继续"、分配成功才提交读、完成经 `update_from_output` 回传），读请求与完成通知走 RPC；
- `storage_interval_log` 事件与守恒断言（∫actual = bytes_read）在服务侧记录并落盘；
- 命中查询走 RPC 保持单一真值源（不做本地副本缓存，避免引入一致性语义）。

### 5.4 回放器与编排（`scripts/replay_http.py`、`scripts/launch_stack.sh`，W4）

- 回放器：读现有 trace JSONL（同格式），按 `arrival_s` 开环发 HTTP，记录每请求 TTFT/E2E/完成状态，输出与 DES `report.json` 可对照的子集字段；
- `launch_stack.sh`：一键起停 frontend / router / N×worker / storage-sim（及模式 2 的 time_server），统一环境（`DYN_DISCOVERY_BACKEND=file`、发现目录、`VLLM_PLUGINS=''`、`HF_HUB_OFFLINE=1`、PYTHONPATH、AB trace 时的 `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`），结束时收集各进程日志与拓扑/env 快照；
- frontend 需要一份 tokenizer 文件（与 `configs/model` 同源的小模型 tokenizer，无权重；W0 准备）。

### 5.5 虚拟时钟服务（`sim/time_server.py`，模式 2，W7，后置）

协议：所有"消耗时间"的参与者——SimExecutor 的计算步、storage-sim 的传输、回放器的到达——向 time_server 登记"虚拟 t 唤醒"；time_server **仅当系统静默时**把时钟推进到最早唤醒点并逐个唤醒。静默判定（保守）：回放器已发出但 worker 尚未确认接收的请求数为 0、无在途存储 RPC。frontend/router 的真实处理时间**计为虚拟 0**（与 DES"无 HTTP/IPC 成本"假设一致）。

显式声明两项残余非确定性：(a) KV 事件（ZMQ 异步发布）相对虚拟时间的滞后不可见——与真实部署的索引滞后一致，但复跑间可能不同；(b) 静默判定若遗漏某条在途消息会产生偶发乱序——只能靠与 DES 对拍发现（A1）。Rust 侧定时器（HTTP 超时、TTL）不纳入虚拟时钟；正常运行下壁钟每虚拟事件仅 ms 级，不会触发。

## 6. 时间推进：两种模式的取舍

| | 模式 1：壁钟 | 模式 2：虚拟时钟服务 |
|---|---|---|
| 实现 | 计算步 `await asyncio.sleep(T)`；storage-sim 按壁钟定时完成 | 登记-静默协议（§5.5） |
| 确定性 | 无（同配置两次跑不同） | 近似（KV 事件滞后 + 静默判定残余） |
| 时序保真 | 受 ms 级 IPC 抖动限制，µs 级事件（如 100 µs 存储启动延迟）失真 | 恢复 µs 级（虚拟时间） |
| 运行时长 | ≈ 虚拟 makespan | 可压缩（不等待真实时长） |
| 工作量 | 含于 W2/W5 | +W7 |
| 适用 | 协议/生命周期/策略生效验证 | 需要对照 DES 或精细时序的实验 |

建议路径：模式 1 先行（W2–W6），模式 2 按研究需要追加（W7）。

## 7. 里程碑与工作量

熟悉本代码库的一个人；估计非排期。

| 里程碑 | 内容 | 验收 | 估计 |
|---|---|---|---|
| W0 环境（mac） | 从固定 SHA `--no-deps` 安装 `ai-dynamo` 组件包（F6）；补 `watchfiles` 等缺包并更新 lock；tokenizer 文件；`file` 发现目录；`doctor.py` 增补对应检查 | 组件可导入、doctor 通过 | 1–2 天 |
| W1 控制面冒烟 | frontend + router + mocker 三进程（F7），HTTP 往返 | 请求成功；确认 tcp 请求面与 ZMQ 事件面在 mac 跑通、无 etcd/NATS 依赖 | 1 天 |
| W2 SimExecutor | §5.1 全部方法；`vllm serve --distributed-executor-backend sim.executor.SimExecutor` 单进程跑 demo 12 请求 | 同 trace 下 schedule 决策序列对齐 DES（A1） | 4–5 天 |
| W3 `dynamo.vllm` 薄补丁 | §5.2 清单；patched/ + apply 脚本 + 指纹记录 | patched 导入、启动，一条请求经 Dynamo 到 vLLM 往返 | 1–3 天 |
| W4 多 worker + 回放器 | N 个 worker 进程；开 `KVEventsConfig` 让 router 真实 cache-aware；`replay_http.py` + `launch_stack.sh` | N worker 稳定服务；KV 事件入索引；AB 子集 trace 注入 | 2–3 天 |
| W5 storage-sim 服务化 | §5.3；SimConnector 改 RPC | 慢存储对照定性复现（A2）；interval 守恒断言 | 3–5 天 |
| W6 验收与文档 | A1–A4 全量；README/DESIGN/VALIDATION/检视报告同步 | 按 AGENTS 规则提交 | 2 天 |
| **小计（模式 1）** | | | **14–21 天 ≈ 3–4 人周** |
| W7 虚拟时钟服务 | §5.5 | 决策序列与 DES 近似逐位对拍（A1 模式 2 版） | +5–8 天 |
| **合计（模式 2）** | | | **≈ 4–5.5 人周** |

依赖关系：W0→W3；W1 仅依赖 W0；W2 可与 W0/W1/W3 并行；W4 依赖 W2+W3；W5 可与 W4 并行；W7 依赖 W2+W5。

## 8. 风险与缓冲

| # | 风险 | 缓冲与对策 |
|---|---|---|
| R1 | `dynamo.vllm` 静态探测（F2）只保证符号存在，`AsyncLLM.from_vllm_config`/`SamplingParams`/`RequestOutput` 的签名与字段在 0.20.2↔0.28 间可能有运行期漂移 | W3 上限 3 天即为此留；逐个以运行期报错驱动修补 |
| R2 | EngineCore 在 empty device 上的初始化链从未被现 DES 走过（现 DES 绕过整条链） | W2 上限 5 天；`_initialize_kv_caches` 每一跳都在 §5.1 有对应桩 |
| R3 | 虚拟时钟静默判定遗漏在途消息 → 偶发乱序 | W7 上限 8 天；验收只能靠 DES 对拍暴露（A1） |
| R4 | Dynamo picker 平局随机（同代价候选随机选）在进程模式同样存在，且失去 DES 的逐位复跑能力 | 不在本设计范围。对照实验判读沿用 AB 结果文档 §6（均值漂 0.3–2.1%、最值漂 1.2–10.9% 的既有量化）；严格对照需 P1 确定性 `(cost, stable_worker_id, dp_rank)` picker，为独立前置项 |
| R5 | macOS 细节：spawn 导入路径、端口占用、`file` 发现目录残留 | launch_stack.sh 统一管理；`--ignore-glob` 证据（F2）表明多模态路径本就非必需 |
| R6 | KV 事件端到端（BlockPool → ZMQ → router 索引）联调 | W4 内含；事件源是真实 BlockPool，机制预期可用（待验证项） |

## 9. 验收标准

沿用总设计 §18.1 的验收哲学：**比较 schedule 决策序列与请求状态，而不是 HTTP 200**。

- **A1 决策序列对拍**：同 trace（demo 12 请求起步，后续 AB trace）下，进程栈与 DES 的每步 schedule 决策序列一致。模式 2 要求近似逐位；模式 1 允许次序扰动，但请求级统计量（TTFT 排序、worker 落点分布）定性一致。
- **A2 慢存储对照**：共享链路 10 → 0.1 GB/s，TTFT 显著劣化，且请求真实经历 `WAITING_FOR_REMOTE_KVS → kv_received → token` 因果顺序（进程侧经 events 记录验证，对应 DES `check_results.py` 的断言移植）。
- **A3 生命周期与记账**：N 请求全部完成、输出数正确；结束时路由 active_requests/prefill/decode 预留归零；KV 块可回收；KV 事件入索引可查询。
- **A4 复现性注记**：模式 1 同配置复跑的指标漂移范围记录在案（方法沿用 AB 结果文档 §6）。
- **产物**：`results/stack_<实验名>/`（report.json、events.jsonl、各进程日志、拓扑与 env 快照、patched diff 哈希），按 AGENTS 规则入 `VALIDATION.md`。

## 10. 适用边界与不做清单

- 不做数值推理、不下载模型权重；SimExecutor 的输出 token 无语义。
- 不 patch Python `time.time()` 去控制 Rust 时钟（总设计 §3.2 已否定）；模式 2 采用登记-静默协议，不改任何时钟。
- 不纳入虚拟时钟的对象：Dynamo Rust 内部定时器（HTTP 超时、TTL 等）——正常运行下不触发；若实验需要长时间暂停虚拟时间，须评估其副作用。
- 不解决 picker 平局随机（P1 独立项，R4）。
- 不实现 PD 分离、TP/PP/多 rank、取消/失败恢复、KV 写回（总设计 §18.2/§18.3 仍为后续；本栈每 worker 单 rank，与现 DES 一致）。
- 平台：macOS Apple Silicon 开发与验证；Linux x86_64 复跑按仓库平台规则择机执行（W0 的 ai-dynamo 安装步骤需写入包内 README，Linux 上可直接用 PyPI wheel，比 mac 更省）。

## 11. 与研究路线的关系

- **在线 MPC 四件套的落地验收场**（MPC 文档 §5.0）：admission 控制接口（`SchedulerQueue` 升级为主动扣留/指令放行）与 StepBatch 事件流（SimExecutor 每步天然可见 SchedulerOutput，是现成挂载点）只有在这样的进程栈上才是"真实部署能建出来的通道"；轻量预测模型与预算化优化的算法内核仍在 DES 上开发校准，迁移时只换观测与控制的通道实现。
- **P1"真 KV 事件桥"在此形态免费获得**：事件源是真实 BlockPool（`KVEventsConfig` 发布 → ZMQ → router 索引），cache-aware 与 storage-aware 的对照自此才成立；DES 侧的 cache-blind 基线保持不变。
- **深拷贝影子世界与定量对照**继续由单进程 DES 承担（`verify_rollout_copy.py` 的定位不变）。

## 12. 关键源码索引

| 目的 | 位置 |
|---|---|
| Executor 抽象与限定名注入 | `upstream/vllm/vllm/v1/executor/abstract.py:48-93`（get_class）、`:81-86`（qualname） |
| EngineCore / EngineCoreProc | `upstream/vllm/vllm/v1/engine/core.py:91`、`:402`（step）、`:806`（Proc） |
| CPU 平台行为 | `upstream/vllm/vllm/platforms/cpu.py:111-214`（F3 各行） |
| Dynamo 发现/请求/事件面 | `upstream/dynamo/lib/runtime/src/distributed.rs:702`、`.../config/environment_names.rs:713`、`.../storage/kv/file.rs:44` |
| Dynamo vLLM 适配层（薄补丁对象） | `upstream/dynamo/components/src/dynamo/vllm/`（`__main__.py`、`worker.py`、`args.py` 等） |
| ai-dynamo 打包 | `upstream/dynamo/pyproject.toml:5,16,64,165-166,235` |
| mocker（W1 用） | `upstream/dynamo/components/src/dynamo/mocker/` |
| 现有 DES 复用来源 | `sim/engine.py`（时间模型/合成输出）、`sim/connector.py`（KVConnectorBase_V1 语义）、`sim/storage.py`（两带宽策略）、`scripts/apply_ascend_patch.py`（补丁落地模式） |
