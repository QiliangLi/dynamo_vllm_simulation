# Windows + WSL2 运行说明

Windows 负责编辑代码和调用入口。WSL2 负责执行原有 Linux CPU 仿真。此适配不改变调度策略、计算时间公式或存储模型，也不提供 Windows 原生 vLLM、CUDA 或 NPU 运行环境。

## 1. 一次性准备

在 Windows 安装可用的 Python 3.10 或以上版本。以下命令使用 `python`；如果本机使用 Python Launcher，可改为 `py -3`。

首次安装 WSL 时，在管理员 PowerShell 执行：

```powershell
wsl --install -d Ubuntu-24.04
```

按 Windows 提示重启。首次打开 Ubuntu，完成 Linux 用户初始化。随后在 Ubuntu 终端中准备系统工具：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv rsync
python3 --version
```

Ubuntu 24.04 的入口需要 Python 3.12。Linux x86_64 是已验证平台；Windows ARM 未验证。安装 WSL、启用虚拟化或系统重启由用户执行，项目入口不会修改 Windows 功能、代理、`.wslconfig` 或管理员权限设置。

## 2. 克隆与安装环境

在 Windows 的 Git 和编辑器中正常克隆 / 打开本仓库。从仓库根目录的 PowerShell 执行：

```powershell
python dynamo_vllm_cpu_sim/scripts/wsl.py setup --stack
```

入口会建立独立的 Linux 代码镜像，安装 uv 0.13.4，运行现有 `bootstrap.sh` 和 `bootstrap_stack.sh`。原有 `versions.json`、`requirements.lock` 和源码指纹继续生效。不更新上游提交。

只需要单进程离散事件仿真时，可使用 `setup`，省略 `--stack`。多进程栈还需要 Dynamo 组件、watchfiles、prometheus-fastapi-instrumentator 和匹配 CPU torch 的 torchvision。Linux 从 PyTorch CPU 索引安装 torchvision，避免误用依赖 CUDA 的 wheel；增量安装不解析或升级原有锁定依赖。

首次安装需要访问 GitHub、PyPI 和 PyTorch CPU 索引。setup 保留 Linux 中的下载代理设置。运行阶段为本地离线服务清除代理变量，并使用 `NO_PROXY=*`，避免内部 metadata HTTP 经过外部代理。该设置只影响此次仿真子进程。

## 3. 检查与运行

```powershell
# 检查固定版本、导入路径和源码指纹
python dynamo_vllm_cpu_sim/scripts/wsl.py doctor

# 运行现有完整测试和 Windows/WSL 同步保护测试
python dynamo_vllm_cpu_sim/scripts/wsl.py test

# 单进程 DES；未指定 --output 时自动建立带时间戳的结果目录
python dynamo_vllm_cpu_sim/scripts/wsl.py run
python dynamo_vllm_cpu_sim/scripts/wsl.py run --scheduler upstream
python dynamo_vllm_cpu_sim/scripts/wsl.py run --config configs/slow_storage.json

# 新多进程框架：原启动、回放、检查与停止流程
python dynamo_vllm_cpu_sim/scripts/wsl.py stack --name windows_remote
python dynamo_vllm_cpu_sim/scripts/wsl.py stack --name windows_local --no-remote-kv --trace configs/stack_smoke.jsonl
python dynamo_vllm_cpu_sim/scripts/wsl.py stack --name windows_slow --sim-config configs/stack_slow.json

# 运行其他现有入口，参数逐项传递，不拼接 shell 命令
python dynamo_vllm_cpu_sim/scripts/wsl.py python scripts/stack_llm_smoke.py
python dynamo_vllm_cpu_sim/scripts/wsl.py python scripts/check_stack.py results/windows_remote results/windows_slow
```

参数中的相对文件路径以 Linux 镜像中的 `dynamo_vllm_cpu_sim/` 为基准。输入文件应放在工程目录内，再使用相对路径；不把 `C:\...` 当作 Linux 路径传递。需要指定输出位置时，使用相对 `results/...` 或 Linux 绝对路径。

原多进程停止脚本会清理同机 EngineCore。因此 `stack` 入口在启动前检查已有 frontend、worker 和 EngineCore；发现其他运行就拒绝启动。请顺序运行实验，不与其他 vLLM / DV 栈并发使用。

多进程命令结束后，入口把 `/tmp/stack_logs/<实验名>/` 中的日志复制到 Linux 工作目录的 `results/<实验名>/logs/`。这样 WSL 自动停止或重启后仍可查看日志。socket 和 PID 文件不会复制。

## 4. 代码同步与结果位置

默认 Linux 工作目录为：

```text
~/.local/share/dynamo-vllm-simulation/<Windows工程路径的哈希>/
```

入口每次打印工作目录和结果目录。可在资源管理器通过 `\\wsl.localhost\Ubuntu-24.04\home\<Linux用户名>\.local\share\dynamo-vllm-simulation\` 查看结果，也可以在 Ubuntu 中打开对应路径。

每次命令执行前，rsync 同步 Windows 工程的当前内容，并移除已在 Windows 删除的旧代码。以下 Linux 运行文件保留，不从 Windows 覆盖，也不会在同步时删除：`.venv/`、`upstream/`、`results/`。入口拒绝覆盖没有匹配来源标记的目录。已有正式实验结果仍保留在 Windows 源仓库；镜像不复制历史 `results/`。

Windows 编辑器通常会把文本转换为 CRLF。`.gitattributes` 为 Python、shell 和 JSON/JSONL 固定 LF；对早于该规则创建的克隆，镜像还会统一启动 shell 脚本的换行。原 Windows 源文件不由入口改写。

入口使用固定上游 SHA 下载 Linux 源码。模型执行仍由 SimExecutor 替代，不因 WSL 支持而变成真实 GPU 推理。Linux empty-device 平台选择只在现有仿真 worker / smoke 入口中补齐，不修改 vLLM 或 Dynamo 源文件。

## 5. 其他发行版或存放位置

`--distro` 和 `--state-root` 放在动作之前：

```powershell
python dynamo_vllm_cpu_sim/scripts/wsl.py --distro Ubuntu-24.04 doctor
python dynamo_vllm_cpu_sim/scripts/wsl.py --state-root /home/alice/dv-runtime setup --stack
```

指定自定义位置后，每次命令都应使用同一个 `--state-root`。推荐使用 Linux 文件系统中的绝对路径，避免把 Python 环境放回 `/mnt/c`。也可通过 Windows 环境变量 `DV_WSL_DISTRO` 指定发行版。

## 6. 常见问题

- 找不到 WSL 或发行版：执行 `wsl --list --verbose`，确认 Ubuntu 已安装且使用 WSL2，再指定正确的 `--distro`。
- 缺少 venv / rsync：在 Ubuntu 安装第一节中的系统工具。
- 下载失败：检查 WSL 内的下载代理和网络，再重跑 setup；不要跳过源码指纹检查。
- `Failed to infer device type`：确认运行的是包含 Linux CPU 平台选择的本仓库仿真 wrapper，而非直接绕过 wrapper 启动 `dynamo.vllm`。
- 没有可服务模型、metadata 请求 502：使用本入口的 `stack`，确认内部请求未经过代理。
- `_C_AVX2`、Triton 或 NIXL 不可用警告：CPU 仿真可能出现这些警告；是否通过以 doctor、实际实验和验收断言为准。

本适配的现场验证记录见 [VALIDATION.md](../dynamo_vllm_cpu_sim/VALIDATION.md)。不同 CPU、WSL 调度和 IPC 会改变壁钟耗时，不能用本机毫秒数代替真实设备性能。
