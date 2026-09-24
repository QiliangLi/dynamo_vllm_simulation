# AGENTS.md

本仓库是"真实 Dynamo 路由 + 真实 vLLM / vLLM-Ascend 调度代码 + 仿真执行"的 CPU 调度实验台工程。本文件是给 coding agent 的工作约定，保持精简；事实与细节一律以下列文档为准。

## 目录导读

| 位置 | 内容 |
|---|---|
| [docs/Dynamo_vLLM_CPU_Simulation_Design.zh-CN.md](docs/Dynamo_vLLM_CPU_Simulation_Design.zh-CN.md) | 总体设计：真实/仿真代码边界、版本固定、存储模型、扩展路线 P1–P3 |
| [docs/CPU真实代码仿真环境检视报告-20260924.md](docs/CPU真实代码仿真环境检视报告-20260924.md) | 实现与需求/研究目标的差距分析、待决策项、后续路线。**改代码前先读** |
| [docs/共享KV多ASU多Path下的组批错峰与带宽协同调度设计-20260917.md](docs/共享KV多ASU多Path下的组批错峰与带宽协同调度设计-20260917.md) | 调度策略研究目标：S/P/B 三层策略、ASU/Path/带宽模型、E26–E31 实验规划 |
| [docs/共享KV统一队列下的Batch效率与错峰调度形式化分析-20260913.md](docs/共享KV统一队列下的Batch效率与错峰调度形式化分析-20260913.md) | 前序形式化分析（09-17 文档已修正其部分结论） |
| [docs/ChatGPT-Ascend路径问题说明-20260924-1858.md](docs/ChatGPT-Ascend路径问题说明-20260924-1858.md) | 方案沟通与交付过程记录 |
| [dynamo_vllm_cpu_sim/README.md](dynamo_vllm_cpu_sim/README.md) | 实现工程的启动方式、可复跑实验、输入输出格式、适用边界 |
| [dynamo_vllm_cpu_sim/VALIDATION.md](dynamo_vllm_cpu_sim/VALIDATION.md) | 已通过的验收记录与数值 |
| `dynamo_vllm_cpu_sim/{sim,scripts,tests,configs,results,patched}/` | 实现代码、引导脚本、测试、配置与验证产物 |

`upstream/`（bootstrap 按 SHA 下载的上游源码）与 `.venv/` 为本地运行产物，已 gitignore，不提交。

## 工作规则

1. **自动提交**：完成并验证一组修改后，直接 `git commit` 并 `push` 到 `origin main`，无需向用户逐次确认（用户已授权）。提交信息用中文概述改动及动机；相关文档更新与代码改动放入同一提交。禁止提交 `upstream/`、`.venv/`、凭据或本地缓存。
2. **文档同步**：修改代码必须同步更新对应文档，缺一不可：
   - 改 `dynamo_vllm_cpu_sim/sim|scripts|configs` → 更新包内 `README.md`；行为或真实/仿真边界变化时，`DESIGN.zh-CN.md` 的 docs 与包内两份副本一起改；
   - 新增/变更验收结果 → 更新 `VALIDATION.md`，原始 `report.json`/`events.jsonl` 存入 `results/<实验名>/`；
   - 差距状态变化（如补齐某策略挂载点、完成 P1 某项）→ 更新检视报告的对应条目；
   - 目录结构、工作规则、版本变化 → 更新本文件。
3. **不伪造结果**：`results/` 与 `VALIDATION.md` 只记录真实运行的输出，禁止手写仿真数值或"预期通过"。上游源码被有意修改后，用 `scripts/doctor.py --allow-edited` 记录，不得假装指纹未变。
4. **版本固定**：三个上游仓库按 `versions.json` 的 SHA 获取并受 `source-fingerprints.json` 校验；升级版本必须同步更新这两个文件、相关文档章节，并在 Linux 环境重跑验收。
5. **平台**：仿真仅在 Linux x86_64（Python 3.12）验证过；macOS/Windows 本机不可直接运行，验证与复跑需在 Linux 服务器/容器/WSL2 进行。

## 常用命令

在 Linux 上、`dynamo_vllm_cpu_sim/` 目录内执行（详见包内 README）：

```bash
bash scripts/bootstrap.sh                # 首次：下载固定源码 + 建环境 + 跑通默认案例
.venv/bin/python -m sim.run --output results/<实验名>          # 运行实验
.venv/bin/python -m unittest discover -s tests -v              # 单元测试
.venv/bin/python scripts/check_results.py results/<实验名>     # 集成断言
```
