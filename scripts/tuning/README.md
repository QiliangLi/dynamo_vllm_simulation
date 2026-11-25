# TFS 自动调优工作流

按 [TFS自动调优工作流设计-20261125.md](../../docs/TFS自动调优工作流设计-20261125.md)（v0.2）实现的无人值守调优。它在四个实例共享 25 GB/s 的仿真上，持续提出、验证、更替通用的 TFS 策略，直到用户叫停。

## 组成

| 位置 | 作用 |
|---|---|
| `tune.py` | 入口：部署工作区、运行守护进程、状态、暂停/继续/停止、给 Agent 用的辅助命令 |
| `tuning/` | 编排器：状态机、运行队列、冻结评价器、门禁、搜索流程、看板 |
| `wsl/run_tuning.py` | WSL 侧的候选运行器：逐步复刻 `scripts/run_4w25.sh`，叠加候选覆盖层，记录与冻结基线的差异，汇总并导出 |
| `wsl/digest.py` | 逐轮诊断摘要：超载时间、饱和时间、读取拉伸、实例空转、等读时间、分类 TTFT、时间线 |
| `wsl/stress_gen.py` | 由冻结输入派生 12 个压力负载（带宽、到达密度、需求分布、实例数），只放沙箱 |
| `tests/` | 单元测试，不需要 WSL 和 opencode：`python -m unittest discover -s tests` |

## 使用

```bash
# 1. 部署（在仓库的 lql_1125_tuning 分支上）。工作区默认放在仓库之外。
python scripts/tuning/tune.py init --workspace D:\Coding\tfs-tuning

# 2. 启动守护进程（双击工作区里的 守护.vbs，或）
python D:\Coding\tfs-tuning\tune.py daemon
```

工作区里的三个双击脚本：`暂停.cmd`、`继续.cmd`、`停止并收尾.cmd`。看板是 `dashboard\index.html`，每分钟自动刷新。

## 硬规则在哪里

- **冻结区**：`tuning/evaluator.py`、`config.py`、`scope.py`、`gates.py`。守护进程启动时比对哈希，运行期间被改动就拒绝评价。
- **候选只能改范围清单内的文件**：见 `tuning/scope.py`。运行器会再核对一次，并要求仿真环境与冻结基线的差异恰好等于候选的覆盖层。
- **每一轮回放都要过仓库自带的校验**：`scripts/run_tfs_stack.py` 的 `verify`、`scripts/check_read_admission.py`。候选必须遵守的约束写在给 Agent 的代码地图里（`tuning/report.py` 的 `CODE_MAP`）。
- **状态与账本**：`state.json`（原子写入）、`ledger.jsonl`（只追加）。

## 模型

主力是 GLM 系列（经本机 opencode 的网关调用）；kimi-k3.1-pro 只在关键环节做交叉验证，每代不超过 5 次。角色定义和提示词在 `tuning/roles.py`，每个任务临时生成自己的 agent 定义，写权限限定在该任务的输出路径。
