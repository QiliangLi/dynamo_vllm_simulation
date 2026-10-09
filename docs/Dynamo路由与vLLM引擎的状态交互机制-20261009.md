# Dynamo 路由与 vLLM 引擎的状态交互机制（真实部署 vs CPU 仿真）- 20261009

| | |
|---|---|
| 日期 | 2026-10-09 |
| 性质 | 讨论结论存档：路由层与推理引擎层之间是否存在状态查询路径、记账机制如何工作、排队情况如何被感知 |
| 相关代码 | `dynamo_vllm_cpu_sim/sim/router.py`、`sim/run.py`、`sim/engine.py`；upstream Dynamo 网关源码（`deploy/inference-gateway/ext-proc/`）与接口声明（`lib/bindings/python/src/dynamo/_core.pyi`） |
| 相关文档 | 设计文档 §5（真实/仿真边界表）、§6（调度闭环）、§17（KV 事件桥）；检视报告 §4.1（策略挂载点缺口）、§8（P1 路线） |

## 0. 一句话结论

**路由器与推理引擎之间没有任何查询路径——这一点真实 Dynamo 部署与我们的仿真一致，是刻意保真的边界。** 路由器手里的引擎"状态"是它对自己经手流量的记账投影（每 worker 两个聚合数字），不是引擎内部状态。真实部署比仿真多出 etcd 服务发现与 KV 事件流两条旁路通道；仿真有意关闭 KV 事件（cache-blind 基线）。策略文档 09-17 所需的"中央调度器看见排队/盘负载/逐层就绪"的状态输入，**在真实 Dynamo 里本来就不存在**——这是要在 P1 新建的能力，不是仿真偷懒省掉的东西。

## 1. 先分清三个角色

讨论这个问题容易混淆的根源在于三个角色在两种形态下的对应关系不同：

| 角色 | 真实部署 | 我们的仿真 |
|---|---|---|
| 路由/调度方 | Dynamo 网关（Envoy ext-proc / 前端组件），独立进程，请求与 token 流都经过它 | `SelectionService`（官方 wheel 里的真实 Rust 选择核心，进程内调用） |
| 推理实例 | worker 进程，内含 vLLM 引擎，经 NATS/HTTP 收请求、流式回 token | 真实 vLLM v1 `Scheduler` Python 对象（进程内，被事件循环驱动） |
| 旁路通道 | etcd（服务发现）+ ZMQ（KV 事件流） | etcd 替换为显式 `upsert_worker` 静态注册；KV 事件关闭 |
| "流经过网关"这件事 | 真实的请求/响应字节流 | `sim/run.py` 事件循环（胶水层）在虚拟时间点上模拟 |

## 2. 真实部署：请求全路径与记账点

```
      client
        │ ① HTTP 请求
        ▼
┌──────────────────────────────┐
│      Dynamo 网关 / 路由        │
│  （Envoy ext-proc / 前端，     │
│     独立进程）                 │
│                              │
│  ② select_and_reserve()      │
│     选 worker，同时记账：      │
│     active_requests         += 1
│     potential_prefill_tokens += N（prompt 长度）
└──────────────┬───────────────┘
               │ ③ 转发请求（NATS / HTTP）
               ▼
┌──────────────────────────────┐
│    worker 进程（推理实例）      │
│  ┌────────────────────────┐  │
│  │ vLLM 引擎               │  │
│  │  waiting 队列 → running │  │
│  │  批 → 逐 token 生成      │  │
│  └────────────────────────┘  │
│                              │
│  ※ 引擎内部状态（waiting 队列、 │
│    running 批、free blocks、  │
│    等远端 KV、抢占）不向网关暴露 │
└──────────────┬───────────────┘
               │ ④ token 流沿原路返回，先经过网关
               ▼
┌──────────────────────────────┐
│      Dynamo 网关 / 路由        │
│  ⑤ 首个 token 经过：          │
│     prefill_complete()       │
│     → 该请求 prefill 负载清零  │
│  ⑥ 响应流结束经过：            │
│     free_reservation()       │
│     → active_requests -= 1  │
└──────────────┬───────────────┘
               │ ⑦ token 返回
               ▼
            client
```

要点：

- 记账发生在**网关侧**，不在 worker 侧。上游源码中 `prefill_complete` 的调用方位于 `deploy/inference-gateway/ext-proc/src/`（`epp.rs`、`selector.rs`、`server.rs` 等），不是 worker 组件——因为网关是唯一同时看得见"请求发出去"和"token 流回来"的角色。
- 接口语义（`_core.pyi` 原文）：`select_and_reserve` = "Select the best worker and book its load"；`prefill_complete` = "load shifts prefill → decode"；`free_reservation` = "releasing its tracked load"；`loads()` = "Current per-model active load (pending counts + per-worker potential loads)"。
- decode 阶段的负载增长由 `add_output_block()` 按输出块增量记账（真实网关会调用）。
- 这个设计的好处：路由决策不需要每次同步查询 worker（无额外 RPC 延迟、不依赖引擎实现）；代价：路由器对引擎内部只有投影没有真相。

## 3. 关键语义：流量投影，不是引擎状态

```
        路由器（选择核心）可见                      路由器不可见
┌───────────────────────────────────┐   ┌─────────────────────────────────────┐
│ 每 worker 两个聚合数字：             │   │ 引擎内部状态：                        │
│  · active_requests（在途请求数）    │   │  · waiting 队列长度与组成              │
│  · potential_prefill_tokens        │   │  · running 批组成                     │
│    （未出首 token 的 prefill 量）    │   │  · 空闲 KV 块数                       │
│                                   │   │  · 是否有请求 WAITING_FOR_REMOTE_KV   │
│ 另有 KV 事件流提供"各 worker 缓存   │   │  · 抢占 / 重算                        │
│ 了哪些前缀块"（缓存感知打分用；      │   │                                     │
│ 本仿真关闭）                        │   │                                     │
│                                   │   │                                     │
│ 来源：路由器自己经手流量的记账       │   │ 来源：只存在于引擎进程内，无任何通道     │
└───────────────────────────────────┘   └─────────────────────────────────────┘
```

"我发过去且未收回来的请求"≈"它在跑的请求"在平时近似成立，但不是一回事：引擎内部的排队、抢占、远端 KV 等待都不会反映到记账里。cache-blind 配置下，选 worker 的打分输入就是这两个数（叠加 KV 事件后才有缓存重叠项）。

## 4. 三条状态通道：真实部署 vs 仿真

| 通道 | 真实部署 | 我们的仿真 | 处理方式 |
|---|---|---|---|
| 负载记账 | 网关观察流事件，调 Rust 选择核心 | `run.py` 在虚拟时间点调同一套真实 Rust 核心 | **保留（真实代码）** |
| 服务发现 | worker 经 etcd 注册、动态上下线 | 显式 `upsert_worker` 静态注册（`router.py:43-55`） | 替换（固定拓扑无需控制面） |
| KV 事件流 | worker 侧发布 BlockStored/Removed，经 ZMQ 进路由器 indexer | 环境变量整体关闭（`router.py:7-10`） | 关闭（未桥接不能伪称 cache-aware） |

## 5. 仿真中的等价实现：事件循环扮演响应流

仿真里没有真实字节流，`run.py` 胶水层在离散事件点上"扮演"了"token 流经过网关"：

```
                run.py 事件循环（胶水层）
        扮演"请求流 / token 流经过网关"的角色
                          │
  请求到达事件 ────────────┤ router.route(row)                 [run.py:93]
                          │   └▶ select_and_reserve()   真实 Rust 核心
                          │       选 worker + 记账 +1/+N
                          │   └▶ settle()               轮询 loads() 直至
                          │                              异步记账收敛（fence）
                          │
  首 token 产生 ───────────┤ router.first_token(rid)           [run.py:63]
                          │   └▶ prefill_complete()     prefill 负载清零
                          │
  请求完成事件 ────────────┤ router.finish(rid)                [run.py:69]
                          │   └▶ free_reservation()     释放请求，-1
                          ▼
        路由器可见：每 worker (active_requests,
        potential_prefill_tokens) —— 与真实部署
        cache-blind 配置下的信息量相同
```

与真实部署的三处实现性差异（不改变记账语义）：

1. **`settle()` 收敛 fence**（`router.py:20-41`）：Dynamo 的 Rust 记账在 Tokio 里异步更新，`await` 返回不等于账本已刷新。胶水层每次调用后轮询 `loads()`，直到与本地维护的期望值逐 worker 一致才推进虚拟时间。这是仿真为确定性加的等待，不计入虚拟推理延迟；真实部署天然按流序处理，不需要这个 fence。该 fence 只覆盖当前使用的记账路径，未控制所有 Rust 时钟（设计文档 §"虚拟时钟"的边界说明）。
2. **`add_output_block()` 未接**：仿真没有增量上报 decode 输出块，首 token 后该请求的负载就不再增长。这是设计文档建议实验保持 `output_tokens < block_size` 的原因；研究长 decode 前要按块边界接反馈并验证原生 decode 记账语义。
3. **引擎内部状态只进产物文件**：waiting/running/free blocks/`WAITING_FOR_REMOTE_KV` 由胶水层记录进 `report.json` 与 `events.jsonl`（`run.py:170-181`、`waiting_remote` 事件字段），供事后分析，不进任何路由决策——与真实部署一致。

## 6. 对研究目标的含义

- 策略文档 09-17 的 S 层（中央组批/错峰/启动）要求调度器看得见"各盘预计负载、逐层 KV 就绪时间"，这类状态输入**在真实 Dynamo 的路由器里本来就不存在**（只有两个聚合数 + 可选 KV 事件）。做存储感知调度不是"打开一个现成开关"，而是新建观测链路。
- 检视报告 §8 的 P1 第一优先级正是这条链路：vLLM 侧动态 admission（waiting 请求是否启动远端读取）→ `Storage.snapshot()` 升级为带 epoch/延迟的观测投影并接入 scorer 输入 → 编译第一个真实 Rust `StorageAwareScorer`。启用真实 KV 事件桥的步骤见设计文档 §17（五步：vLLM 侧取事件 → 配置 `kv_events_endpoint` → 虚拟完成事件与 ingestion watermark → 丢失/重放/重启处理 → 验收）。
- 论文/报告表述边界：本仿真的路由行为可表述为"真实 Dynamo 选择核心 + cache-blind 配置 + 生命周期记账桥接"；不能表述为"完整 Dynamo 服务栈"或"缓存感知路由"。

## 7. 代码位置索引

| 内容 | 位置 |
|---|---|
| cache-blind 环境变量（KV 事件/复用假设/overlap 信用/温度全关） | `sim/router.py:7-11` |
| SelectionService 实例化 | `sim/router.py:17` |
| settle() 收敛 fence | `sim/router.py:20-41` |
| upsert_worker 静态注册 | `sim/router.py:43-55` |
| route / first_token / finish | `sim/router.py:57-82` |
| 三个生命周期调用点（胶水层） | `sim/run.py:93`、`run.py:63`、`run.py:69` |
| 引擎状态只进产物文件 | `sim/run.py:170-181`、`waiting_remote` 事件字段 |
| 真实侧 prefill_complete 调用方（网关） | `upstream/dynamo/deploy/inference-gateway/ext-proc/src/{epp,selector,server,picker,epp_router}.rs` |
| SelectionService 接口语义声明 | `upstream/dynamo/lib/bindings/python/src/dynamo/_core.pyi:736-826` |
| 自定义 Rust 策略接口（后续 S 层挂载点） | `upstream/dynamo/lib/kv-router/src/scheduling/selector/policy.rs` |
