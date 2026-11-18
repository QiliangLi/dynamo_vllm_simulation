# 服务器 synthetic_abc 与仿真运行指南

本文用于同事拉取仓库后，在 Linux / WSL 中重放服务器同一份请求形状和到达序列。当前对照对象为 `benchmark_tools_20261116/catalogs/synthetic_abc.json`。运行入口为 `dynamo_vllm_cpu_sim/scripts/run_server_benchmark.py`。

## 1. 服务器用的是哪一个 benchmark

服务器此前跑过 `mooncake_abc`，后来为固定长度、分析读算压力，使用 `synthetic_abc`。两者不可混用结果。这里同步的是交付服务器的本地维护版，没有直接读取服务器核对额外修改。

| 名称 | replay 数量 | 特点 | 最后计划到达 |
| --- | ---: | --- | ---: |
| `synthetic_abc`（本文主测） | 360，ABC 各120 | 每类固定 H/U | 17 s |
| `mooncake_abc` | 360，A240/B90/C30 | 同类请求长度不同 | 18 s |
| `pilot24` | 24，A16/B6/C2 | Mooncake 抽样，用于启动冒烟 | 18 s |
| `synthetic_abc_3w` | 120，ABC 各40 | B/C 的 U 改为8192，100 ms间隔 | 11.9 s |

`synthetic_abc` 固定输入如下。H 为预期外部缓存命中 token 数，U 为新增计算 token 数。

| 类别 | H | U | 输入 H+U | 数量 |
| --- | ---: | ---: | ---: | ---: |
| A | 30720 | 256 | 30976 | 120 |
| B | 9344 | 2048 | 11392 | 120 |
| C | 0 | 384 | 384 | 120 |

块大小128，每条最多生成1个 token。请求按 catalog 的 `arrival_offset_ms` 发送，包含同一时刻到达的突发，不是均匀速率。17秒是最后计划到达时间，不是整轮完成时间。A/B/C 是负载标签，不直接决定 TFS 的运行期重读分类。

服务器的统一命令如下。在已经启动默认策略服务的 `drt_dev` 中执行：

```bash
python3 -u benchmark_tools_20261116/benchmark.py run \
  --benchmark synthetic_abc \
  --endpoint http://127.0.0.1:8000/v1/completions \
  --prepare-concurrency 3 --concurrency 360 \
  --replay-phase2-dump off --label default
```

TFS 组先按服务器启动脚本切换服务，再使用同样命令，将标签改为 `--label tfs`。标签只命名结果，不切换策略。`--concurrency 360` 是客户端在途上限，不是模型 batch size，也不把360条请求改成同时发送。

prepare 为 A/B 共240条请求准备前缀，通常输入 H+1 个 token。准备完成后等待10秒，随后按原到达序列 replay。上述开关显式启用 prepare 保存、关闭 replay 新增 KV 保存，GET 保留。prepare 失败的对应 replay 会跳过；其他请求继续，报告保留失败和跳过数量。HTTP成功不证明 OD 写入或命中成功。

## 2. 仿真与服务器对齐到什么程度

| 项目 | 当前仿真 |
| --- | --- |
| 请求负载 | 读取服务器同一份 catalog，保持类别、H/U、数量、顺序、到达时刻、1-token 输出 |
| 输入内容 | 用 CPU 仿真 tokenizer 生成等长输入；不是服务器相同的文本或 token ID |
| Dynamo | 仓库内维护版1.2源码，真实 frontend、KV路由及资源预留 |
| vLLM | CPU环境的0.20.2 EngineCore、AsyncScheduler、KV分配；不是在CPU执行NPU算子 |
| 实例 | 3个，每实例最多1条运行请求，token预算50000，上下文98000 |
| 计算 | MiniMax TP2 / batch1 / 16层的 `prefill50k_total16_v2` 拟合 |
| 存储 | 6盘共享25,000,000,000 B/s，单盘25/6 GB/s，65536字节/token |
| 缓存准备 | 直接预置前缀并发布 DISK 事件，不执行真机 prepare 写入过程 |
| 回放保存 | `enable_phase2_dump=false`，与上述服务器 replay 开关对应 |
| 计时边界 | 比较热缓存 replay 的客户端TTFT和整轮完成时间；不比较prepare耗时 |

散列分盘、每路径1 GB/s、100 µs启动延迟、每实例5900个KV块是当前仿真假设。三个实例使用相同计算模型，不能还原真机实例速度差异、RDMA故障和物理盘效率。完整配置见 [minimax_server_3w.json](../dynamo_vllm_cpu_sim/configs/minimax_server_3w.json)。

## 3. 首次安装与编译

使用 Linux x86_64 或 Windows 的 WSL2 Linux 终端。建议在 Linux 文件系统（例如 `~/work`）克隆和构建。需要访问 GitHub、PyPI、PyTorch及Rust包源。不需要模型权重、NPU、CUDA运行服务、etcd或NATS。

以下从克隆开始。在已有仓库时跳过 clone，进入仓库根目录后继续设置变量。更新已有工作树时先处理本地修改，再 `git pull --ff-only`。本指南先在 `drt_develop` 发布，随后随该分支合入 `main`，两者均可使用。

```bash
git clone --branch drt_develop https://github.com/QiliangLi/dynamo_vllm_simulation.git
cd dynamo_vllm_simulation
export REPO="$PWD"
export DV="$REPO/dynamo_vllm_cpu_sim"
export PY="$DV/.venv/bin/python"
export BUILD="$HOME/.cache/dynamo-server-sim/build-1.2"
export RESULTS="$HOME/.cache/dynamo-server-sim/results"

# Ubuntu / Debian 的系统依赖；已有依赖时跳过安装。
sudo apt-get update
sudo apt-get install -y build-essential pkg-config libssl-dev protobuf-compiler \
  clang libclang-dev cmake curl git unzip

# 已安装 uv / rustup 时跳过对应安装命令。
curl -LsSf https://astral.sh/uv/install.sh | sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
rustup toolchain install 1.96.1 --profile minimal
export RUSTUP_TOOLCHAIN=1.96.1

cd "$DV"
bash scripts/bootstrap.sh

# 补齐进程栈的导入依赖；server runner 使用下方构建的1.2组件。
uv pip install --python "$PY" watchfiles prometheus-fastapi-instrumentator
uv pip install --python "$PY" --no-deps \
  --index-url https://download.pytorch.org/whl/cpu 'torchvision==0.26.0'
"$PY" scripts/apply_ascend_patch.py

bash scripts/build_server_dynamo.sh "$REPO/source_dev/dynamo" "$BUILD" "$PY"
uv pip install --python "$PY" --target "$BUILD/python" --no-deps \
  'nixl==0.10.1' 'nixl-cu12==0.10.1'

export PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS=""
```

`bootstrap.sh` 建立Python3.12环境，安装固定CPU依赖和vLLM源码，并运行基础检查。环境中的Dynamo1.5为旧入口所用；server runner优先加载独立构建的1.2，不需要卸载1.5。

Rust构建使用dev profile、优化等级2，默认4个编译任务。日志在 `$BUILD/build.log`，可以在另一终端 `tail -f` 查看。内存不足时在构建命令前加 `CARGO_BUILD_JOBS=2`。首次构建需要下载依赖并编译，后续同一源码可复用构建结果。更新Dynamo源码后须重建；只改α/dev或catalog不需要重编译。

新终端需要重新设置上面 REPO、DV、PY、BUILD、RESULTS 和运行环境变量，并 `cd "$DV"`。不要同时启动多组仿真；当前入口共用28765/28766及29111—29113端口。仿真脚本会启动并清理本轮存储、central、3个worker和frontend，不需要另外手动启动服务。

## 4. 先冒烟，再跑服务器原版 ABC

每次使用新的输出目录。`--mode both` 先default后TFS；两组复用本次生成的同一份输入。

```bash
# 快速确认环境和三个实例，不能作为360条性能结论。
"$PY" scripts/run_server_benchmark.py \
  --server-dynamo "$BUILD/python" \
  --catalog "$REPO/benchmark_tools_20261116/catalogs/pilot24.json" \
  --mode both --alpha 0.1 \
  --output "$RESULTS/pilot24_$(date +%Y%m%d_%H%M%S)"

# 主测：保持服务器 synthetic_abc 的原始到达序列。
"$PY" scripts/run_server_benchmark.py \
  --server-dynamo "$BUILD/python" \
  --catalog "$REPO/benchmark_tools_20261116/catalogs/synthetic_abc.json" \
  --arrival-pattern original --arrival-scale 1 --wall-time-scale 1 \
  --mode both --alpha 0.1 \
  --output "$RESULTS/synthetic_abc_$(date +%Y%m%d_%H%M%S)"
```

主测命令不带 `--dev`，TFS启用正常重读额度。也可使用 `--mode default` / `--mode tfs` 分别运行。`--arrival-pattern periodic-abc` 会改变到达序列，属于另一个实验，不能称为服务器原版ABC复现。`--limit` 只用于缩短启动检查，不能拿子集结果与360条结果直接比较。

## 5. 测 TFS 控制路径开销：α=1 + dev

```bash
"$PY" scripts/run_server_benchmark.py \
  --server-dynamo "$BUILD/python" \
  --catalog "$REPO/benchmark_tools_20261116/catalogs/synthetic_abc.json" \
  --arrival-pattern original --arrival-scale 1 --wall-time-scale 1 \
  --mode both --alpha 1 --dev \
  --output "$RESULTS/synthetic_abc_dev_$(date +%Y%m%d_%H%M%S)"
```

α=1不折扣等待重读评分；dev使重读额度无限。TFS请求登记、重读判定、ReadGate同步申请、许可记账与生命周期反馈仍执行。此时不会因重读额度耗尽而等待，但通信耗时仍会改变请求抵达时刻和排队情况，因此不保证逐请求路由结果与默认策略相同。

该命令同时配置仿真frontend与central。真机使用这组对照时，也要保证frontend和central均设置α=1、central启用dev；只给benchmark设置label不能完成切换。

## 6. 查看结果与比较

在结果目录中查看 `comparison.json`：

```bash
# 将路径替换成刚输出的本轮目录。
"$PY" -m json.tool "$RESULTS/本轮目录/comparison.json"
```

| 字段 / 文件 | 含义 |
| --- | --- |
| `groups.all`、`groups.A/B/C` | 各组请求数、成功数、可见首文本数、平均/P95 TTFT（ms） |
| `elapsed_model_s` | replay开始至最后结束的模型时间（s），不是进程启动耗时 |
| `read_bytes` | 实际提交的仿真读取字节 |
| `default/config.json`、`tfs/config.json` | 有效模型、存储、α、dev配置 |
| `default/report.json`、`tfs/report.json` | 逐请求结果和计时 |
| `tfs/admission.events.jsonl` | 分类、许可申请、授予与释放 |
| 各组 `layer_check.json`、`storage.final.json` | 逐层读算和存储检查 |

原版ABC完整命中时，两组应各360/360，提交读取315,076,116,480字节。先核对完成数和字节，再比较TTFT。真机对应 `replay/summary.md` / `summary.json`，并保留 `arguments.json`、失败和跳过数量。不要把prepare耗时混入replay完成时间；不同失败/跳过数量意味着有效负载不同。

当前没有冻结的真机SLO基线，不用仿真配置内的占位阈值宣称服务器SLO达标。仿真用于筛选负载和定位机制，绝对TTFT仍需真机校准。

## 7. 已有验证与范围

2026-11-18交付代码已通过421项仿真测试、11项服务器benchmark测试及237文件同步检查。从交付目录运行pilot24，default/TFS均24/24完成，复用既有独立Dynamo1.2构建。此前原版synthetic_abc也已在WSL完成default/TFS各360条，结果见[三实例说明](服务器三实例仿真.md)。

本指南核对了脚本接口、catalog和命令语法；没有在空白机器重新下载并编译所有依赖。网络、系统包和可用内存仍由运行机器提供。其他源码与历史运行说明见[包内README](../dynamo_vllm_cpu_sim/README.md)。
