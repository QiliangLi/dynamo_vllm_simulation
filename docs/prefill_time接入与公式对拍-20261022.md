> 2026-10-28后续：正式GLM批次估时已切换实际工作量v2；下文为历史最大长度版本。
> 新模型、固定布局和画像v3见[prefill_time实际组批接入](prefill_time实际组批公式接入-20261028.md)。

# prefill_time 接入与公式对拍设计

## 目标与来源

在 `多进程tfs实验` 分支接入 GLM5 + D950PR 的 prefill 时间模型。保留线性模式。单进程离散事件入口和多进程 SimExecutor 使用同一个计算模块。只修改本仓库，不升级 vLLM、Dynamo 或 Ascend 固定版本。

多进程模块取自本机已保存的 DV 提交 `dcbb4e58af260a58e1c562392e5aa69cb4a9d1d1`。原始时间模型取自 SA 提交 `79f48a097e9f3b10fb6b805f1d6671bc5bec5809` 的 `prefill_time/prefill_time_batch.py`。复制并冻结源码及 SHA256，不在运行中跨仓库导入。

## 计算语义

新增 `compute.mode=glm5_d950pr`。旧配置默认 `linear`，保留旧结果。

1. 从真实 Scheduler 的本轮结果获取每个请求的已计算上下文 `h` 和新增 prefill token 数 `u`。
2. 公式输入为 `(input_length=h+u, u_tokens=u)`。长请求的前一个 chunk 不能提前使用完整 prompt 长度。
3. 保留来源脚本的异构 batch padding 上界：取 `max(h+u)`、`max(u)`、实际 prefill 请求数 `bs`。IO 量按各请求求和。日志同时保留原始形状和 padding 后形状。
4. 保留来源的参考布局 `dp=max(1,32//bs), ep=dp, tp=1`，显式记录。这是每步的解析模型布局，不能等同于本次启动的逻辑 worker 数，也不能用来预测真实动态重分片开销。每批最多 8 个请求；超过时拒绝执行。
5. 模型时间为 `3*T_dense + 75*T_moe`，单位由 us 转 s。来源的 `per_layer_us` 是上述总时间除以 78。模型已含部分 kernel 启动开销，不重复添加旧 `step_overhead_s`；可另设 `engine_overhead_s`，默认 0。
6. DV 在计算前完成外部前缀 KV 接收。本模式默认 `sparse_HBM_hit_ratio=1`，关闭原公式额外的 storage_bw 拉取项。独立的 DV 存储模型继续负责远端前缀读取。HBM 读取及 KV 复用假设仍保留。可显式改变该参数用于公式验证，日志必须记录。
7. 本次验证限定 prefill-only，输出 1 token。首 token 在最后一个 prefill chunk 返回。本模式若看到 decode token 就报错，不将 `u=1` 冒充 decode 计时。没有实现完整 PD 移交协议。
8. TFS 的单请求参考计算量同步使用同一模型，按参考 chunk 预算分解。它是到达时估计，不能读取未来组批结果。默认带宽需求由参考读取量/参考计算时间获得，不继续暗用线性 prefill 系数。

## 实验与独立校验

从本机 Mooncake `conversation_trace.jsonl.hit.jsonl` 选择少量请求，保留行号、原字段和源文件 SHA256。trace 只有块标识，没有实际 token；生成可复现的合成 token ID，明确这一限制。`hit_tokens` 作为预置远端缓存假设，按 DV 块边界向下对齐；输出长度改为 1。到达节奏是测试条件，不冒充原始全 trace 回放。

至少覆盖：单请求、异构 batch、长请求分块、远端前缀命中。先运行离散事件模式，再运行真实多进程 EngineCore + mock Executor。记录 Scheduler 实际给出的每批形状、模型分项、预期 sleep、实测 sleep 和执行墙钟。多进程不保证 HTTP 同时提交就形成指定 batch，只检查实际批次。

独立复算脚本不得导入生产计时函数或来源算子函数。按固定常量逐项计算 FLOPs、访存字节、roofline、流水下限、启动和通信。逐步对比 `3*T_dense+75*T_moe`，并输出可阅读的算式表。区分模型时间、sleep 实耗、HTTP 延迟和起栈耗时，不要求墙钟等于解析时间。

## 验收

- 旧线性时间模式回归通过；新模式输入检查、bs=1 与来源一致、异构 padding、chunk 上下文、decode 拒绝均有测试。
- 独立公式与程序记录逐步相符，误差使用浮点容差，不用墙钟误差掩盖公式偏差。
- 全部请求完成，远端 KV 接收发生在依赖计算之前，进程由运行脚本回收。
- 更新 README、DESIGN 两份、VALIDATION 和本次结果报告。结果必须来自实际运行。
- 本次不提交或推送。设计和公式不代表已完成 D950PR 硬件校准；TFS 多进程中央准入仍不在本次范围。
