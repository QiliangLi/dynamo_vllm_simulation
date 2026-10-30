# TFS与Mooncake360实验分支

本分支提供真实Dynamo路由、vLLM调度、仿真模型/共享存储，以及固定benchmark目录。
先读[实验分支使用说明](docs/实验分支使用说明.md)，AI先读[AGENTS.md](AGENTS.md)。
输入及类别到达规则见[Benchmark目录](benchmark/benchmark目录.md)，通用用法见[benchmark说明](benchmark/README.md)。本分支不含模型权重或大规模原始运行数据。

已归档报告见[报告目录](report/报告目录.md)，通用规则见[report/README.md](report/README.md)。报告按benchmark分组，子目录列明创建时间与实际比较方案；历史模型和研究版运行边界在各报告开头标注。

可共享的自编技能放在[skills](skills/README.md)。用途、依赖和入口见[技能目录](skills/技能目录.md)，AI使用与维护见[agent.md](skills/agent.md)。

---

# Dynamo / vLLM CPU 调度仿真

本仓库使用真实 Dynamo、vLLM 和部分 vLLM-Ascend 调度代码。模型执行与存储传输由仿真模型替代。

- [工程说明与 Linux / macOS 运行方式](dynamo_vllm_cpu_sim/README.md)
- [Windows + WSL2 安装与启动](docs/Windows-WSL2运行说明.md)
- [版本与验证记录](dynamo_vllm_cpu_sim/VALIDATION.md)

Windows 可以直接克隆仓库并在编辑器中打开。仿真通过 WSL2 / Ubuntu 24.04 执行；不需要 Windows 原生安装 torch、vLLM 或 CUDA。

准备好 WSL2 和 Python 后，从仓库根目录的 PowerShell 执行：

```powershell
python dynamo_vllm_cpu_sim/scripts/wsl.py setup --stack
python dynamo_vllm_cpu_sim/scripts/wsl.py doctor
python dynamo_vllm_cpu_sim/scripts/wsl.py run
python dynamo_vllm_cpu_sim/scripts/wsl.py stack --name windows_remote
```

每次启动都会把 Windows 中编辑的工程代码同步到独立的 Linux 工作目录。Python 环境、固定上游源码和结果保留在 Linux 文件系统。不同克隆的环境与结果互相隔离。
