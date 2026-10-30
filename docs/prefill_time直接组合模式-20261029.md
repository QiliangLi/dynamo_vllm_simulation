# prefill_time 直接组合模式

2026-10-29。新增可选的 `glm5_direct_sum_v1`，用于“批内各请求单独执行时间之和，仅减少重复算子启动”的估计。原默认 `glm5_actual_batch_work_v2` 保留。

## 计算规则

每条请求使用本步真实形状 `(h,u)`：`h` 是已计算或命中的上下文，`u` 是本步新计算 token。单请求沿用正式 v2 公式。所有请求使用同一组固定 TP/DP/EP 配置。

设批内有 `n` 条活跃请求，`f` 为启动开销合并比例：

```text
T_batch = sum(T_singleton_i) - max(n-1, 0) × f × 3 × 7 µs × 78
```

扣减仅涉及三个已经明确计入 `kernel_launch=7 µs` 的算子：

- `indexer_score_us`
- `gather_us`
- `sparse_attention_us`

这三个算子在每层各计一次启动开销。`f=1` 表示每个算子在合批后保留一次启动；`f=0.5` 表示只合并一半重复开销；`f=0` 表示严格累加各请求。5 条请求在 `f=1` 时共减少 **6.552 ms**。

权重读取、专家路由、dispatch/combine 的 30 µs 启动项、算子计算量、流水线下限及可选稀疏取数均按单请求累加。直接组合不额外计入权重复用收益，不用最长长度补齐，也不随 batch 大小改变部署。

`engine_overhead_s` 属于已有的每个真实执行 step 开销，仍只加一次。公式中的 `T_singleton_i` 只含模型层时间，不含该额外引擎开销。逐层执行在每层分别求和和扣减，维持 3 个 Dense 层与 75 个 MoE 层。

## 配置与调用

在实验 JSON 的 `compute` 中设置：

```json
{
  "mode": "glm5_d950pr",
  "batch_time_model": "glm5_direct_sum_v1",
  "launch_saving_fraction": 1.0,
  "tp": 1,
  "dp": 8,
  "ep": 8
}
```

未指定 `launch_saving_fraction` 时，直接组合模式取 1。比例必须是有限数且在 `[0,1]` 内。旧模式不接受该选项，以免配置被静默忽略。未指定 `batch_time_model` 的配置继续使用 v2。

直接调用组批函数时，输入长度是本步结束上下文长度 `h+u`：

```python
from sim.timing_models.prefill_time_batch import estimate_batch

result = estimate_batch(
    [(4096, 4096), (512, 512), (512, 512), (512, 512), (512, 512)],
    dp=8, ep=8,
    batch_time_model="glm5_direct_sum_v1",
    launch_saving_fraction=1.0,
)
print(result["total_ms"])
print(result["batch_composition"])
```

`per_batch()` 支持相同选项，仍返回“平均每层微秒、每层前缀 KV GB”。最多 8 条请求的边界不变。`u=0` 的行保留前缀容量，不计入活跃请求数，也不产生启动收益。

## 正式执行与审计

调用链为 `estimate_step` → `GLM5ComputeTimeModel` → `estimate_batch` → 各请求 v2 单请求估计 → 逐层求和与扣减 → `LayerRunner` 或 bulk 执行。

timing 记录 `batch_time_model=glm5_direct_sum_v1`，并在 `batch_composition` 保存单请求公式版本、合并比例、扣减项、单请求总时间和扣减微秒数。`moe_work.singletons` 保存独立请求的专家工作量，避免把各请求的重复权重读取当成一次共享读取。

`scripts/check_prefill_formula.py` 使用独立的固定常数算术复核。无版本的历史记录和已有 v2 记录仍按各自公式审计。

单请求没有重复启动，因此两种模式的单请求时间完全一致。请求画像仍使用 `glm5_reference_v3`，参考分块、计算压力、期限与 SLO 阈值保持原值。调度、准入、路由、KV 分配和存储带宽规则不变。

真实两实例功能验收命令：

```bash
PYTHONHASHSEED=0 .venv/bin/python scripts/validate_prefill_batch.py \
  --batch-time-model glm5_direct_sum_v1 \
  --launch-saving-fraction 0.5 \
  --output results/<新目录>
```

## 证据边界

这是用户指定的保守组批假设。7 µs 沿用冻结参考常数，合并重复启动的比例是实验参数。当前没有 GLM5/D950PR 实机计时来确认实际节省值。它不保证拟合所有真实批次，也不能用于解释真实 GPU 的全部组批收益。

本次只验收新计时模式，不重跑 Mooncake360 五方案收益矩阵。验收结果与数值例子记录在包内 `VALIDATION.md` 和 `results/prefill_direct_sum_20261029/`。


## 本次数值与验收

固定 TP1/DP8/EP8、无前缀，时间单位为 ms：

|新token列表|单独执行合计|直接组合 f=1|直接组合 f=0.5|原v2聚合|
|---|---:|---:|---:|---:|
|4096,512,512,512|1068.6109824|1063.6969824|1066.1539824|901.18525248|
|4096,512,512,512,512|1205.17647936|1198.62447936|1201.90047936|981.94217280|
|512,512,512,512,512|682.8274848|676.2754848|679.5514848|411.21602880|

完整268项单元测试通过。两真实实例的bulk直接与逐层TFS各8请求通过，存在真实
多请求批次和分块，独立算术最大误差1.11e-16秒。这里比较的是模型公式，不是实测GPU延迟。
