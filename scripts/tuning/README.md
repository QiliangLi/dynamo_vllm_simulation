# TFS 自动调优工作流

按 [TFS自动调优工作流设计-20261125.md](../../docs/TFS自动调优工作流设计-20261125.md)（v0.2）实现的无人值守调优。它在四个实例共享 25 GB/s 的仿真上，持续提出、验证、更替通用的 TFS 策略，直到用户叫停。哪些设计点已实现、哪些没有，见设计文档的「实现状态」一节。

## 组成

| 位置 | 作用 |
|---|---|
| `tune.py` | 入口：部署工作区、运行守护进程、状态、暂停/继续/停止、给 Agent 用的辅助命令 |
| `tuning/daemon.py` | 守护进程：状态机、运行队列、第 0 代、WSL 常驻会话与存活检查 |
| `tuning/search.py` | 一代的流程：诊断、提案、红队、门禁、冒烟、筛选、确认、审计、裁决、复盘；收尾 |
| `tuning/evaluator.py` | 统计与裁决规则。冻结：启动时比对哈希 |
| `tuning/scope.py`、`gates.py` | 候选可以改什么（范围清单）、静态门禁。冻结 |
| `tuning/roles.py`、`llm.py` | 角色定义、提示词、任务书；经 opencode 调用模型 |
| `tuning/report.py` | 给 Agent 读的文字摘要、代码地图；看板 |
| `tuning/gitops.py`、`guard.py` | 在独立工作树里登记冠军与每代小结；任务后还原越界改动 |
| `wsl/run_tuning.py` | WSL 侧的候选运行器：逐步复刻 `scripts/run_4w25.sh`，叠加候选覆盖层，记录与冻结基线的差异，汇总并导出 |
| `wsl/digest.py` | 逐轮诊断摘要 |
| `wsl/stress_gen.py` | 由冻结输入派生 12 个压力负载，只放沙箱 |
| `tests/` | 单元测试，不需要 WSL 和 opencode：`python -m unittest discover -s tests` |

## 使用

```bash
# 1. 部署（在本分支上执行；把分支导入 WSL 作为冻结基线）。工作区放在仓库之外。
python scripts/tuning/tune.py --workspace D:\Coding\tfs-tuning init

# 2. 启动（双击工作区里的 守护.vbs，或命令行）
python D:\Coding\tfs-tuning\tune.py daemon

# 状态；暂停、继续、停止并收尾
python D:\Coding\tfs-tuning\tune.py status
python D:\Coding\tfs-tuning\tune.py control pause | resume | stop
```

工作区里的三个双击脚本：`暂停.cmd`、`继续.cmd`、`停止并收尾.cmd`。看板是 `dashboard\index.html`，每分钟自动刷新。部署时会在用户的「启动」文件夹放一个指向 `supervisor.vbs` 的快捷方式，登录后自动恢复运行；「停止并收尾」之后它不再启动。

守护进程崩溃后由 `supervisor.vbs` 在一分钟内重启。停止它的正规办法是「停止并收尾」，不要直接杀 WSL 里的回放进程。

## 运行中的注意事项

- **WSL 会空闲停机。** 没有会话挂着时，WSL 会在几分钟后停掉整个发行版，连同里面的回放。守护进程持有一个常驻会话防止这一点，并每分钟检查运行器是否还活着；消失就记失败并补跑。
- **opencode 的项目目录取自环境变量 `PWD`/`OLDPWD`**，不是子进程的工作目录；每个任务使用私有服务（`--standalone`），启动时同步扫描角色定义。角色定义必须按 LF 写入。
- **回放一次只跑一轮。** 候选要和串行测出的基线在同样的主机条件下比。实测（2026-11-26）两路并行时 light 基线在 408 上 TTFT 高 7.7%、完成时间高 3.1%，mooncake、AgentX 和 540 在噪声内；而晋升规则不允许任何一个负载有超出噪声的回退。第 0 代因此对每个并行档位探测全部核心负载，任何一个负载的偏移超过 2.5% 就不采用该档位。不要手工把并行度调高。
- **重新部署**：改了 `scripts/tuning/` 之后，运行 `python scripts/tuning/tune.py --workspace D:\Coding\tfs-tuning init --skip-wsl`，再重启守护进程。动了 `evaluator.py`、`config.py`、`scope.py`、`gates.py` 还要先 `python tune.py control unfreeze` 登记新哈希，**即使部署当时守护进程没有重启**：它只在校验启动时的字节，下一次重启才会发现不一致并拒绝启动，守护脚本连续五次后会停止拉起。`init` 会在输出里提示「冻结文件与工作区登记的哈希不同」。

## 硬规则在哪里

- **冻结区**：`tuning/evaluator.py`、`config.py`、`scope.py`、`gates.py`。守护进程启动时比对哈希，运行期间被改动就拒绝评价。
- **候选只能改范围清单内的文件**：见 `tuning/scope.py`。运行器会再核对一次，并要求仿真环境与冻结基线的差异恰好等于候选的覆盖层。改到 Rust 侧的候选目前被拒绝，因为运行器不会重新编译 Dynamo。
- **每一轮回放都要过仓库自带的校验**：`scripts/run_tfs_stack.py` 的 `verify`、`scripts/check_read_admission.py`。候选必须遵守的约束写在给 Agent 的代码地图里（`tuning/report.py` 的 `CODE_MAP`）。
- **Agent 的权限**：写权限限于任务的输出路径；命令先全部拒绝，再放行该任务需要的。提案角色可以运行 `python tune.py`；只读角色只能列目录和读文件，外加运行经过编排器验证的分析脚本（`tools/`）；审计角色可以运行自己目录下的脚本。
- **状态与账本**：`state.json`（原子写入）、`ledger.jsonl`（只追加）。

## 模型

主力是 GLM 系列（经本机 opencode 的网关调用）；kimi-k3.1-pro 只在关键环节做交叉验证，每代不超过 5 次。角色定义和提示词在 `tuning/roles.py`。
