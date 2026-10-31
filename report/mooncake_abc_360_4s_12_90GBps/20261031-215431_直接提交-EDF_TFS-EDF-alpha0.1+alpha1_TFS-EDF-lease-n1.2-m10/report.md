# mooncake_abc_360_4s_12_90GBps：存储带宽租约第四臂（tfs-lease）四臂对照

同轮四臂对照（一次 run_benchmark 全量运行、同一冻结输入与 native 构建）：直接提交 EDF、TFS EDF α=0.1、TFS EDF α=1 与新增 TFS EDF lease。lease 臂实现带宽派生轻重线（L=0.1×盘带宽）、每盘 B/s 租约账本五步放行、读开始/末层读毕事件释放与双信号路由折扣 f=1/m；本轮参数为调参定标值（判定线 1.2×、m=10）。

## 0 结论（先结论）
- SLO 最优臂：TFS EDF α=0.1（31.7%）；mean TTFT 最优臂：TFS EDF α=0.1（1122 ms）。
- lease 臂：mean 1996 ms、p95 6232 ms、SLO 31.1%、完成 10175 ms。
- 相对默认参数（1.1×/m=4，r1 同轮）：mean 2194→1996 ms，SLO 31.7%→31.1%。
- 单轮运行存在轮间噪声（同配置 mean 差可达 ±15%~30%）；本文数值不含重复轮方差，结论方向以跨负载一致的趋势为准。

## 1 汇总与 SLO
| 臂 | mean TTFT (ms) | p95 TTFT (ms) | SLO % | 完成窗口 (ms) | 读取字节 |
|---|---|---|---|---|---|
| 直接提交 EDF | 1657 | 3310 | 12.2 | 7882 | 539571462144 |
| TFS EDF α=0.1 | 1122 | 3565 | 31.7 | 7261 | 539571462144 |
| TFS EDF α=1 | 1174 | 3464 | 26.1 | 7200 | 539571462144 |
| TFS EDF lease(n1.2×m10) | 1996 | 6232 | 31.1 | 10175 | 539571462144 |

SLO 为每请求 TTFT≤4×新 T0（nearest-rank p95；K=1）。正式 360 条；预热 72 条已排除。共享池 90 GB/s（读自 benchmark config/manifest）。

## 2 时间线
- 到达窗口 4 s；43 个计划时刻；正式窗口重置 GC。
- 完成窗口（最后首 token − 最早到达）：直接提交 EDF 7882 ms；TFS EDF α=0.1 7261 ms；TFS EDF α=1 7200 ms；TFS EDF lease(n1.2×m10) 10175 ms。

## 3 计算占比与租约机制
- lease 臂模型计算总量 53.4 s / 完成窗口 13.0 s = 4.12（12 逻辑实例并行下的等效占用约 4.1 个实例；非单实例占比）。
- 租约：发放 223（正常路 209、例外路 4、空账护栏 10）；重读消息 4.0 条/条（申请RTT+读开始+读毕）；获准→读开始虚占窗口均值 8.5 ms。
- 拒绝分布：例外带 8584、条件(a) 42、条件(b) 969、加载位 0；账面峰值 173.6 GB/s（例外带 135）。
- 释放：lease_completed 223、first_token 释放 0（须为 0）、TTL 回收 0；对账 209 次。

## 4 带宽
- 读取总量 539.571 GB，四臂一致（输入冻结）。
- 盘峰值速率（vs 容量 90 GB/s）：直接提交 EDF 90.0；TFS EDF α=0.1 90.0；TFS EDF α=1 90.0；TFS EDF lease(n1.2×m10) 90.0 GB/s。
- 速率与账本均为仿真量（grouped max-min 注水），非实机测量。

## 5 TTFT 分布
- 逐请求 TTFT 见 report_data/<arm>_ttft.csv.gz；四臂 mean/p95 对比见 figure/。

## 6 机制与前序对照
- 调参扫描（mean ms / SLO%，跨轮均值，n=轮数；详见第 7 节噪声说明）：
| 配置 | abc_90 | abcd_90 | random_90 | n |
|---|---|---|---|---|
| 默认1.1/1.5 m4 | abc 2400 / 28.3 | abcd 2650 / 24.5 | random 970 / 32.6 | 2-3 |
| m10（1.1/1.5） | abc 2325 / 31.6 | abcd 1866 / 33.0 | random 1096 / 34.3 | 4 |
| n1.2/m10（选定） | abc 2059 / 32.8 | abcd 1737 / 31.1 | random 943 / 31.8 | 3 |
| n1.5/e2.5 | abc 1962 / 30.0 | abcd 1547 / 32.8 | random 1124 / 20.6 | 1 |
| n2.0/e2.5 | abc 1508 / 32.5 | abcd 1262 / 34.4 | random 1226 / 20.0 | 1 |
- 选定 n1.2/m10：mean/p95/完成时间全面优于 m10 与默认，SLO 在轮间噪声内；random 对判定线 ≥1.5× 有 SLO 悬崖（同刻峰值需求≈1.48×容量），不得放宽。
- 机制修正：条件(a)只保护"不加候选仍可达标"的在读者（已注定超期者不再否决），否则少数巨请求长期否决全部新进入。

## 7 限制
- 单轮对照：同配置轮间 mean 差实测 ±15%~30%（abcd 同配置 2099↔3129 ms），小于该量级的差异不应解读为优劣。
- 仿真边界：GLM5 解析计时与存储速率属于仿真；消息走本机回环，不代表跨机。
- lease 合同限定单共享池（disks=1）；多盘仅结构预留。
- 未验证项（沿用设计文档 §10）：协议失败路径的对抗验证、跨机部署。

## 8 附表
- TFS EDF α=0.1：申请 RPC 均值 0.25 ms、p95 0.50 ms、拒绝 3554 次、回滚 0。
- TFS EDF α=1：申请 RPC 均值 0.32 ms、p95 0.95 ms、拒绝 2974 次、回滚 0。
- TFS EDF lease(n1.2×m10)：申请 RPC 均值 1.81 ms、p95 4.92 ms、拒绝 9595 次、回滚 0。

## 9 验证与复现
- 本轮全部 20 组 driver status=passed；各组 check.json/layer/hit/EDF/评分/read_admission/lease_accounting/warmup 检查全部通过（见源运行目录）。
- 复现（WSL，仓库根 dynamo_vllm_cpu_sim）：
```bash
.venv/bin/python scripts/run_benchmark.py --case all --arm all \
  --dynamo-python build/weighted-dynamo-8/python \
  --output results/benchmark_runs/<新目录>
```
- 源运行：lease-final-r3（调参默认）与 lease-formal-r1（默认参数基线），位于运行机 results/benchmark_runs/；调参轮 tune-* 共 14 轮保留。

## 10 解读
- lease 臂把"何时允许重读开读"从条数计数改为带宽账本：SLO 与 mean 的取舍取决于负载形态。本负载上 lease 未同时拿到 mean 与 SLO 双最优；其价值在于事件驱动记账（虚占窗口 ~7ms vs 首token释放）与护栏的可观测性。
- 判定线是唯一需要按负载定标的敏感参数；本报告选定 1.2× 作为跨负载折中。
