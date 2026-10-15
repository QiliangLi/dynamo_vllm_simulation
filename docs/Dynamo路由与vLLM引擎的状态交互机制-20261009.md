# Dynamo 路由与 vLLM 引擎的状态交互机制（真实部署 vs CPU 仿真）- 20261009

| | |
|---|---|
| 日期 | 2026-10-09 |
| 修订 | 2026-10-14：**勘误**——原文"路由器与引擎之间没有任何查询路径"准确范围是**逐请求路由决策路径**；补充 Dynamo 的第三条通道 FPM 每步事件平面（§6），新增 llm-d、AIBrix 交互机制与三框架对照（§7–§9） |
| 性质 | 讨论结论存档：路由层与推理引擎层之间是否存在状态查询路径、记账机制如何工作、排队情况如何被感知 |
| 相关代码 | `dynamo_vllm_cpu_sim/sim/router.py`、`sim/run.py`、`sim/engine.py`；upstream Dynamo 网关源码（`deploy/inference-gateway/ext-proc/`）与接口声明（`lib/bindings/python/src/dynamo/_core.pyi`） |
| 相关文档 | 设计文档 §5（真实/仿真边界表）、§6（调度闭环）、§17（KV 事件桥）；检视报告 §4.1（策略挂载点缺口）、§8（P1 路线）；`vLLM组批规则与调度器并发推演可行性-20261010.md`（StepBatch 事件流需求，本文 §6/§10 的上游先例对照）；`MPC调度架构前提与真实Dynamo-vLLM落地挑战-20261010.md`（事件流观测四要素之一） |
| 交互接口全景图 | [docs/figures/orchestrator-vllm-interfaces.html](../figures/orchestrator-vllm-interfaces.html)（交互式架构图，2026-10-15 生成：三框架组件与全部接口连线，含 Dynamo 三通道、插件挂载点与 llm-d/AIBrix 对照组；规格源文件同目录 `.json`） |
| 调度打分明细图（单页三图） | [docs/figures/scheduler-formula-all.html](../figures/scheduler-formula-all.html)（2026-10-15 生成，单页含三张独立架构图，每图配导读、"符号×含义×来源"对照表与公式外注记）。三框架统一按四层呈现：① proxy+决策插件同构结构（Envoy + ext_proc/EPP/路由插件）；② 调度公式本体——Dynamo 为 kv-router `default.rs:282-388` 的 logit 加性公式（s·P′+D+w·A，argmin+平局 softmax 采样）、llm-d 为 EPP 三步插件链（tokenize→亲和过滤→argmin 在途 token）、AIBrix 为 load-balance 容量感知加权等策略族；③ 公式每个因子的获取通道（记账/KV 事件/PromQL/自记账/render）；④ 公式外交互以虚线画出（etcd/FPM→Planner、pod 发现与 Ready 门控、Redis 同步等）。图规格源文件同目录 `scheduler-formula-{dynamo,llmd,aibrix}.json`（archify validate 9/9 通过） |

## 0. 一句话结论

**逐请求路由决策路径上，路由器与推理引擎之间没有任何查询路径——这一点真实 Dynamo 部署与我们的仿真一致，是刻意保真的边界。** 路由器手里的引擎"状态"是它对自己经手流量的记账投影（每 worker 两个聚合数字），不是引擎内部状态。但 Dynamo 整体并非"与 vLLM 没有接口交互"：它另有两条**异步观测通道**——KV 事件流（缓存内容，喂路由打分）与 FPM 每步事件流（含 waiting 队列的每步指标，喂扩缩容 Planner，不进路由决策，见 §6）。llm-d 与 AIBrix 同样不做逐请求同步查询，都是"订阅/抓取 → 本地缓存 → 逐请求打分"模式（§7–§9）。策略文档 09-17 所需的"中央调度器看见排队/盘负载/逐层就绪"的状态输入，**在三个框架的逐请求路由路径里都不存在**——这是要在 P1 新建的能力，不是仿真偷懒省掉的东西。

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
| FPM 每步事件流 | InstrumentedScheduler 每步 forward 后 ZMQ PUB ForwardPassMetrics（含 waiting 队列）→ Planner 扩缩容（§6） | 未使用（仿真不含 Planner，也未接 FPM） | 未接（P1 观测通道的现成蓝本） |

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

## 6. 勘误与补充：Dynamo 的第三条通道——FPM 每步事件平面（2026-10-14）

本文初版基于仿真所用的 SelectionService 路径，得出"Dynamo 与 vLLM 之间几乎只有记账"的印象，**这是不完整的**。复查 `upstream/dynamo/components/src/dynamo/vllm/` 后，完整图景是三条通道并存：

```
              Dynamo 1.5.0 与 vLLM 的三条通道（本地源码核实）

┌────────────────────────────────────────────────────────────────┐
│ 通道 1｜数据面 · 在途记账（§2 详述）                                │
│   网关侧：reserve → prefill_complete → free_reservation           │
│   消费者：逐请求路由打分（SelectionService）        [零滞后·投影]   │
├────────────────────────────────────────────────────────────────┤
│ 通道 2｜KV 事件流（缓存内容）                                      │
│   vLLM BlockStored/Removed ──ZMQ──▶ 路由器 indexer                │
│   消费者：逐请求路由打分（cache-aware 项）          [事件滞后·真实] │
├────────────────────────────────────────────────────────────────┤
│ 通道 3｜FPM 每步事件平面（本文初版遗漏）                            │
│   InstrumentedScheduler（--scheduler-cls 注入进 vLLM）            │
│   每个 forward 步完成 ──ZMQ PUB──▶ ForwardPassMetrics：           │
│     · scheduled_requests（本批实际调度了哪些请求）                  │
│     · queued_requests（发射时刻 self.waiting 的长度）              │
│     · wall_time（本批 GPU 前向耗时）                              │
│   消费者：Planner（SLA 驱动扩缩容控制器）            [每步级·真实]  │
│   ✗ 不进逐请求路由决策                                             │
└────────────────────────────────────────────────────────────────┘
```

关键事实：

- `instrumented_scheduler.py` 的注入方式正是 vLLM 官方的 `--scheduler-cls` 扩展点——**在调度器内部、每个前向步完成后发布事件**；序列化与 ZMQ 发送走后台线程，不占调度热路径（文件头注释自述了 sync/async 两种引擎模式的发射点差异）。
- Planner 用 FPM 做"负载驱动扩缩容"与在线回归（TTFT/ITL SLA 目标），支持 vLLM/SGLang/TRT-LLM 多引擎、含 attention-DP 每 rank 信号（`planner/README.md`）。
- 因此"Dynamo 看不到 vLLM 排队情况"的准确表述是：**看得到（FPM 每步都有 waiting 队列长度），但只接到扩缩容，不接路由**。路由打分输入仍只有记账 + KV 事件两项。

对本工程的直接意义：10-10 组批规则文档提出的"每步 StepBatch 事件流接口"，上游已有生产先例——注入方式（scheduler 子类）、传输（ZMQ PUB）、字段（每步调度集合/队列长度/批耗时）三者齐备。P1 观测通道可按此模式在虚拟时钟下重建（见 §10）。

## 7. llm-d 的交互机制（K8s 原生，Gateway API Inference Extension）

数据来源：llm-d 官方 Precise Prefix Cache Routing 指南（2026-10-14 检索）。

```
                        llm-d 逐请求路由路径

client ──HTTP──▶ Envoy ──ext_proc──▶ EPP（Endpoint Picker，路由插件容器）
                                      │
                                      │ ① discoverPods=true：为每个就绪
                                      │    pod 建 ZMQ 订阅器（端口 5556）
                                      │ ② render 服务（调 vLLM /v1/*/render）
                                      │    把 prompt 精确 tokenize 成块序列
                                      │ ③ prefix-cache-affinity-filter：
                                      │    候选收窄到"前缀块常驻"的 pod；
                                      │    暖集吞吐饱和后回退低载 pod
                                      │ ④ token-load-scorer：
                                      │    在途 token 负载最小者胜出
                                      │    （EPP 本进程记账，单副本部署）
                                      ▼
                              选定 pod（vLLM / SGLang）

各 pod ──KV 事件（ZMQ 5556：BlockStored/Removed）──▶ 所有 EPP 副本
         └─ 各自维护"常驻 KV 块索引"，多副本收敛到同一索引
各 pod ──重放缓冲（ZMQ ROUTER 5559，约 1 万条批量）──▶ EPP 重启后重建索引
```

要点：

- **缓存内容观测走 vLLM KV 事件**（与 Dynamo 通道 2 同源同模式），且带**重启重放缓冲**——这是"观测通道需要 watermark/重放"的一个工程先例。
- **负载观测走 EPP 自身在途 token 记账**（与 Dynamo 通道 1 同构：路由器只信自己经手的流量）；指南明确因此该账本是每进程的，此模式下 EPP 单副本部署。
- 不抓取引擎 Prometheus 指标进路由决策；放置层（K8s scheduler 插件）另行处理，不在逐请求路径。
- 失败回退显式分层：前缀暖集饱和（超过标定的 peakPrefillThroughput）即退回低载 pod——"缓存优先、负载兜底"。

## 8. AIBrix 的交互机制（Envoy Gateway + ext_proc 插件）

数据来源：AIBrix 官方文档 gateway-plugins 与 prefix-cache-aware-routing 页（2026-10-14 检索）、AIBrix 论文（arXiv:2504.03648）。注：此前讨论中提及的"GGWP"名称在现行官方文档中已查无对应组件，本节以核实后的插件体系为准。

```
                        AIBrix 逐请求路由路径

client ──HTTP──▶ Envoy Gateway ──ext_proc gRPC──▶ AIBrix 路由插件
                                                │
                                                │ 策略族（HTTPRoute 按
                                                │ model 头匹配，逐请求选择）：
                                                │  random / least-request /
                                                │  least-kv-cache /
                                                │  load-balance /
                                                │  prefix-cache / slo-* /
                                                │  pd / session-affinity …
                                                ▼
                                           选定 pod

Prometheus ◀──（周期抓取 vLLM /metrics）── 各 pod
    │
    └─PromQL 查询（PROMETHEUS_ENDPOINT）──▶ 插件消费：running/queued
                                            请求数、KV 占用等（多策略共用）
网关自记账（在途请求、EWMA 输出 token 速率）──▶ load-balance 打分
前缀索引（默认模式）＝ 网关自己的路由历史哈希表   ──▶ prefix-cache 策略
前缀索引（KV 事件同步模式，可选）＝ 引擎 BlockStored/Removed 事件
        （AIBRIX_PREFIX_CACHE_KV_EVENT_SYNC_ENABLED=true 时启用；
          多网关副本间可用 Redis 共享索引 AIBRIX_STATESYNC_ENABLED）
```

要点：

- **指标来源是"PromQL 抓取 + 网关自记账"双通道**：load-balance 打分 = (running + λ·queued) / EWMA(输出速率) × KV 空闲惩罚项，KV 占用低于 10% 空闲直接出局。KV 指标缺失按全空闲处理、token 速率缺失用他 pod 均值——大量"缺数据兜底"逻辑，说明抓取通道的覆盖与新鲜度是现实痛点（`slo-least-load-pulling` 策略更是专为"拉新鲜快照避免过期缓存"而设）。
- **前缀索引默认模式是路由历史的投影，不是引擎真实缓存**——文档原话："索引反映网关把请求发去了哪，不反映引擎现在还缓存着什么"；LRU 上限 20 万块、20 分钟无刷新过期。要真相需打开 KV 事件同步模式。这与本文 §3"投影 ≠ 真相"的论断完全同构，且是已投产系统踩过并修正的坑。
- 论文层（arXiv:2504.03648）：控制面含 LLM 专属 autoscaler（滑动窗指标聚合内置在 autoscaler、绕过 custom-metrics 链路）与 AI Runtime sidecar（指标标准化）；数据面网关按 KV 局部性/吞吐/异构性做逐请求路由。

## 9. 三框架对照：同一模式，不同取舍

**共同模式：没有任何框架在逐请求热路径上同步查询引擎。** 三者都是"异步订阅/周期抓取 → 汇入本地状态 → 逐请求打分"，差别只在喂给本地状态的通道及其新鲜度/精度：

| 维度 | Dynamo 1.5.0 | llm-d | AIBrix |
|---|---|---|---|
| 在途负载来源 | 网关记账（reserve/free，零滞后） | EPP 在途 token 记账 | 网关自记账 + PromQL running/queued |
| 缓存内容来源 | KV 事件（真实） | KV 事件（真实，含重启重放） | 默认=路由历史投影（失真）；可选 KV 事件同步（真实） |
| 引擎排队指标进路由 | 否（FPM 只喂扩缩容） | 部分（暖集饱和阈值） | 是（queued/running/KV 占用进多策略打分） |
| 每步级事件流 | 有（FPM：每步调度集合/队列长度/批耗时 → Planner） | 无 | 无 |
| 抓取滞后处理 | 不适用（记账零滞后） | 不适用（事件+自记账） | 显式兜底：缺失默认值、pulling 拉新鲜、负载失衡前置门控 |
| 扩缩容观测 | FPM 事件平面 | K8s 侧另行处理 | autoscaler 内置滑动窗聚合 |

各自强处：Dynamo 记账零滞后、FPM 每步粒度最细；llm-d 的 KV 事件索引带重放缓冲、缓存路由生产验证充分（官方博客报告 57× TTFT 案例）；AIBrix 策略族最全（公平性 VTC、SLO 感知、P/D、会话亲和），且"投影索引失真 → 事件同步模式"的演化路径是最有价值的反面教材。vLLM 上游的 KV Events API 是三家共用的标准化观测接口。

结论：**不存在单向的"Dynamo 不如 llm-d/AIBrix"**。三者在逐请求路由上的信息量同量级（记账/事件 + 缓存索引）；Dynamo 的差异化优势恰是 FPM 这种每步级事件流——但它目前只接到扩缩容，没接到路由。

## 10. 对研究目标的含义

- 策略文档 09-17 的 S 层（中央组批/错峰/启动）要求调度器看得见"各盘预计负载、逐层 KV 就绪时间"，这类状态输入**在三个框架的逐请求路由路径里都不存在**。三个框架的实践共同印证：先有观测通道，才有状态感知调度——不是"打开一个现成开关"。
- **P1 观测通道有现成蓝本**：Dynamo 自带的 InstrumentedScheduler/FPM 证明"注入 scheduler 子类 → 每步发事件 → 控制器消费"在生产可行。10-10 组批规则文档提出的 StepBatch 事件流、10-10 MPC 文档"控制器只吃事件流不直读引擎对象"的约束，均可按此模式落地：`--scheduler-cls` 注入点（vLLM 官方扩展点，本仿真 `engine.py:38-56` 已按同机制加载 BalanceScheduler）→ 虚拟时钟下的每步事件 → 带 epoch/滞后的观测投影。
- **观测滞后与投影失真是现实工程问题**：AIBrix 默认前缀索引的失真教训、`slo-least-load-pulling` 的"拉新鲜"设计、llm-d 的重放缓冲与 watermark，都是 09-17 文档"带 epoch/延迟/噪声的 StorageSnapshot"设计合理性的旁证。
- 检视报告 §8 的 P1 第一优先级正是这条链路：vLLM 侧动态 admission（waiting 请求是否启动远端读取）→ `Storage.snapshot()` 升级为带 epoch/延迟的观测投影并接入 scorer 输入 → 编译第一个真实 Rust `StorageAwareScorer`。启用真实 KV 事件桥的步骤见设计文档 §17（五步：vLLM 侧取事件 → 配置 `kv_events_endpoint` → 虚拟完成事件与 ingestion watermark → 丢失/重放/重启处理 → 验收）。
- 论文/报告表述边界：本仿真的路由行为可表述为"真实 Dynamo 选择核心 + cache-blind 配置 + 生命周期记账桥接"；不能表述为"完整 Dynamo 服务栈"或"缓存感知路由"。

## 11. 代码位置索引

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
| Dynamo FPM 每步事件发射器（`--scheduler-cls` 注入点） | `upstream/dynamo/components/src/dynamo/vllm/instrumented_scheduler.py` |
| Dynamo FPM 消费者（SLA 扩缩容 Planner） | `upstream/dynamo/components/src/dynamo/planner/`（`README.md`、`core/perf_model/engine_query.py`） |
| Dynamo worker 侧 KV 事件发布 | `upstream/dynamo/components/src/dynamo/vllm/main.py`、`state_agent.py` |

## 12. 外部资料（2026-10-14 检索）

- llm-d：[Precise Prefix Cache Routing Guide](https://github.com/llm-d/llm-d/blob/main/guides/precise-prefix-cache-routing/README.md)（EPP 插件链、ZMQ 5556/5559、重放缓冲、回退逻辑）；[KV-Cache Wins You Can See](https://llm-d.ai/blog/kvcache-wins-you-can-see)（57× TTFT 案例）；[Red Hat: Master KV Cache Aware Routing with llm-d](https://developers.redhat.com/articles/2025/10/07/master-kv-cache-aware-routing-llm-d-efficient-ai-inference)
- AIBrix：[Gateway Routing Plugins](https://aibrix.readthedocs.io/latest/features/gateway-routing/gateway-plugins.html)（策略族、PromQL 指标来源、兜底逻辑）；[Prefix-Cache Aware Routing](https://aibrix.readthedocs.io/latest/features/gateway-routing/prefix-cache-aware-routing.html)（默认索引=路由历史投影、KV 事件同步模式、Redis 状态同步）；[AIBrix 论文 arXiv:2504.03648](https://arxiv.org/html/2504.03648v1)（autoscaler 指标聚合、sidecar 指标标准化）
- vLLM：[Metrics Design](https://docs.vllm.ai/en/stable/design/metrics)（/metrics gauge 族，AIBrix 等抓取通道的原始来源）
