# 07 验证：服务器 synthetic_abc 场景（cap=1 复现与修复）

日期：2026-11-18。上游：合并 origin/main（e1d3ac4，含 drt_develop 491a1cb）。
入口：`scripts/run_server_benchmark.py`（本次新增 `--tfs-json` 覆盖入口，
仅作用于 tfs 模式，default 模式不受影响）。
指南：`docs/服务器SyntheticABC与仿真运行指南.md`。

## 1. 场景与静态预判（predict_cap.py，未跑完整栈）

synthetic_abc：360 条（A/B/C 各 120），3 实例（每实例 1 运行请求）、
6 盘共享 25 GB/s、65536 B/token、MiniMax TP2 拟合计时。

| 类 | H | 读字节 | 计算 | pressure |
|---|---|---|---|---|
| A | 30720 | 2.013 GB | 53.7 ms | **37.50 GB/s** |
| B | 9344 | 0.612 GB | 102.3 ms | 5.99 GB/s |
| C | 0 | 0 | 49.5 ms | 0 |

全体 P75 分位（含 C 类 0）落在 A 段：θ=37.5 GB/s > 参考带宽 25 GB/s。
**cap = floor(25/37.5) = 1**。用户描述的"额度限到 1 个"在此负载上成立：
3 实例中 2 个会因全局 cap=1 空转等额度；A 类即使独占全部带宽也需要
80.5 ms 读 > 53.7 ms 计算，属于结构性读受限。

## 2. 变体与预判

| 变体 | 等效行为 | 预判 |
|---|---|---|
| tfs legacy | cap=1 | 重读全局串行，TTFT 大概率劣于 default |
| tfs + overdraft(1s,+2) | 稳态 cap=1 + 等待超 1s 超发至多 2 | 填满 3 实例；削长尾 |
| tfs + fixed 3 | cap=3（=实例数） | 实例全并行，存储 3 路并发 |
| tfs + measured | 实测占用中位数分母 | **可能自我锁死**：cap=1 串行时实测≈独占速率 24 GB/s → cap 仍 1 |
| tfs + dev | 无限额度 | 指南既有对照（控制路径开销） |
| default | immediate（不限额） | 基线 |

measured 锁死机制（待验证）：实测窗口=授予→首 token≈纯读时间（独占 25 GB/s），
rate≈24 GB/s，cap=floor(25/24)=1。负反馈在极端点失效——cap 从未放宽过，
实测速率从未被并发摊薄过。

## 3. 环境与构建记录

- WSL Ubuntu-24.04 `/root/dv-repo`，全量同步含 source_dev/benchmark_tools/
  minimax_prefill（首版同步漏了包根 minimax_prefill，已补）。
- 新装 rustup + 1.96.1 工具链、watchfiles；torchvision 已有；
  nixl 进 build-1.2/python。
- `build_server_dynamo.sh` 首次构建（Dynamo 1.2，Rust dev profile）。
- 服务器栈端口 28765/28766/29111-29113，单轮独占。

## 4. 实验结果（2026-11-18 13:14-13:26 连续执行）

环境就绪过程（溯源）：首次构建失败——842b925 提交漏了 7 个源文件，其中
mocker 三个 `core.rs` 被上游 `.gitignore` 的 `core.*` 规则误伤（其余 4 个
不在编译树）。从上游 ai-dynamo/dynamo v1.2.0 补齐 mocker 三件后构建通过
（fork 的 mocker 与上游仅 router.rs 一处差异，补文件安全；已 git add -f
入库修复）。nixl 需装进 build-1.2/python。

### E-S0 冒烟（pilot24，24 条）

default 24/24 mean 138ms；tfs 24/24 mean 143ms。读 15.703GB，
6 盘各峰值 4.167GB/s（=25/6，正确）。环境通过。

### E-S1 复现（synthetic_abc 360 条，读 315.076GB 精确匹配指南预期）

| 臂 | mean TTFT | p95 | A 类 | B 类 | C 类 |
|---|---|---|---|---|---|
| default（direct） | 3042ms | 4803ms | 3232 | 3009 | 2886 |
| tfs legacy | 2204ms | **7253ms** | **5010ms** | 913 | 690 |

tfs legacy 事件：**cap 收敛 1**（cap_median=1，θ=37.5GB/s 与静态预判一致），
denied 2840 / grants 126（22.5:1），等额度中位 **4661ms**、p90 7245ms。

**复现成立**：A 类 TTFT 比 direct 差 55%、p95 差 51%——"额度限到 1 导致
排队抵消收益"确认；B/C 被 cap=1 错峰保护（mean 大幅优于 direct）。

### E-S2 变体（同晚连续）

| 臂 | mean | p95 | A | B | C | 等待中位/p90 | 机制 |
|---|---|---|---|---|---|---|---|
| tfs + od(1s,+2) | 2573 | **5134** | **3276** | 2454 | 1988 | 2669/4504 | **超发 106 次，denied 2840→160** |
| tfs + fixed3 | 2556 | 5124 | 3153 | 2471 | 2043 | 2581/4332 | denied 11 |
| tfs + measured | 1946 | 7339 | 4779 | 645 | 416 | 4049/6923 | **锁死：cap=1**（实测中位 15.63GB/s→floor(25/15.63)=1） |
| tfs + dev（无限） | 2721 | 5218 | 3251 | 2540 | 2372 | 2644/4541 | denied 0 |

### E-S3 敏感性（od 0.5s,+2）

运行成功（360/360，mean 2792ms、p95 4874ms）但该轮验收/汇总步骤异常，
admission 事件文件不完整，数据以 report.json 手动汇总为准。
0.5s 阈值较 1s：p95 更好（4874 vs 5134）、mean 更差（2792 vs 2573），
1s 版本均衡更优。

## 5. 判读

1. **overdraft(1s,+2) 有效解决本场景问题**：A 类 5010→3276ms（**-35%**，
   追平 direct 的 3232），p95 7253→5134（-29%），mean 仍显著优于 direct
   （2573 vs 3042）。三类公平化：B/C 让出部分被保护利益（913→2454），
   换取 A 类不再被卡死。
2. fixed3 效果与 od 相当（A 3153 甚至略好），但需要人工定标 cap 值；
   od 不需要知道"正确 cap"，稳态保守 + 按需超发，自适应突发。
3. **measured 锁死被实验证实**：cap=1 串行下实测占用≈独占速率的 60%
   （15.63GB/s），cap=floor(25/15.63)=1 自我锁定，负反馈在极端点失效。
   measured 不适用于 cap 极低场景（与 04 文档 Mooncake 场景结论互补）。
4. dev（无限额度）= od/fixed3 的上界参照：三者 A 类均 ~3200ms，
   说明 od(+2) 已接近"无限制"的可达水平；od 的优势是保留了稳态额度
   语义与 B/C 保护。
5. elapsed 全部 ≈17.3s：315GB/25GB/s=12.6s 传输下界 + 17s 到达窗口，
   完成时间无差异空间；改进全部体现在 TTFT 分布的公平性上。
6. legacy 的低 mean（2204）是把 A 类牺牲到 5s 换来的——mean 指标在此
   场景掩盖了尾部不公平，评估应看分类均值与 p95。
