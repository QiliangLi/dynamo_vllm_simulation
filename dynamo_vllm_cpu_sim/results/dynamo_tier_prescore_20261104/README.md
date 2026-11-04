# 前置分级画像功能验收

本目录只保存两实例功能小测，不是性能实验。

- smoke-r2：α=0.1，同覆盖候选，8/8 完成，12 行当前候选评分产生折扣。
- asymmetric-r1：两候选的 Disk 事件覆盖不同，8/8 完成，6 行当前候选评分产生折扣。
  tier_prescore.strengthened.json 是补充的逐候选检查，原 validation.json 保持原样。
- alpha1-r1：不同覆盖，8/8 完成，当前候选折扣为 0。实际命中偏差 3 条被允许并记录。
- bias-half-r1：预计 512、实际 1024 token，8/8 完成，6 个请求记录偏差。
- legacy-r1：关闭分级画像，12/12 完成，旧等待折扣和原生资源账本归零。
- unit.log：363 项 Python 回归通过，96.480 秒。
- build.json：固定源码的独立原生扩展 dev/debug=0 构建记录。

frontend.prescore.log 是原日志中与准备、评分、booking 和选择报告有关的原样行摘录。
`[ROUTING] Best` 是 native 调度返回后的宿主日志，不代表 picker 的实际调用时点。
源码调用顺序为 scorer → picker → 完整 booking。日志检查确认 prepare 早于首次 scorer，
所有候选评分早于 booking。heavy=false 不扣当前增量；α=1 的 weighted 与 baseline 相等。

保留的失败预试：smoke-r1 的 8 条执行全部完成，但旧审计错误地要求宿主 Best 日志早于
booking。修正审计后另跑 smoke-r2；未覆盖原记录。完整失败日志保存在本机
/home/zed/.local/share/state-aware-network/audits/dynamo-tier-prescore-20261104/smoke-r1。
构建目录、二进制、大型原始数据不随本目录提交。没有运行 GPU 或完整性能矩阵。
