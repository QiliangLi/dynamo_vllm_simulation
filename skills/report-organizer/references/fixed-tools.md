# 固定报告工具

以下命令从仓库根目录执行。技能复制到其他位置后，将脚本路径替换为对应位置。
数据路径和输出路径由调用者提供，不依赖原作者的工作区、账户或运行目录。

## 环境

校验标准dataset使用Python标准库。绘图还需Matplotlib、NumPy、Pillow和可显示中文的系统字体。
Python至少3.10。需要安装依赖时，由用户显式执行：

```bash
python -m pip install -r skills/report-organizer/requirements.txt
```

优先使用当前Python。缺绘图库时，可用 `REPORT_ORGANIZER_PYTHON` 指定另一个已有解释器；工具不会自动搜索个人环境或安装依赖。
中文字体使用Arial Unicode MS、Noto Sans CJK SC、思源黑体、微软雅黑等可用字体，不附带字体文件。

Windows默认使用Windows Python，不强制要求WSL。需要使用WSL时显式设置 `REPORT_ORGANIZER_WSL_DISTRO` 为已安装的发行版名称；此时依赖在该WSL环境中准备。
同时设置解释器变量时，该值须为WSL内解释器路径。脚本不创建或升级WSL环境。

## 最小样例

```bash
python skills/report-organizer/scripts/generate_report.py \
  --dataset skills/report-organizer/examples/dataset.json --validate-only

python skills/report-organizer/scripts/generate_report.py \
  --dataset skills/report-organizer/examples/dataset.json \
  --output temp/report-skill-example
```

样例是人工构造的格式测试数据，不代表Dynamo/vLLM运行结果。输出目录必须不存在。

## 实验数据入口

```bash
python skills/report-organizer/scripts/generate_report.py 已有报告目录 --output 新报告目录
python skills/report-organizer/scripts/generate_report.py --manifest cases.json --output 新报告目录
python skills/report-organizer/scripts/generate_report.py --dataset dataset.json --output 新报告目录
```

这些命令读取已完成日志，不启动实验。DV逐层日志需要配置、正式请求集合、状态、时钟和逐层计算/读取记录。
跨目录清单有 `schema_version:1`、`title`、`cases`；case包含 `id`、`path`，可选 `label`、`color`、`run_metadata`。
输入位置与配置不受支持时，按 [标准数据结构](report-schema.md) 转换；没有证据的字段不能补0。

增加 `--validate-only` 可只校验数据。人工分析通过 `--narrative analysis.json` 提供，键为 `0/6/7/8/10`，值为相应章节的Markdown正文。
`execution_boundary`应区分实验实际执行与当前离线报告阶段；不要把生成器未启动仿真写成原实验未执行。

## 图表与交付

固定七类图为SLO柱状图、在线轻重时间线、模型计算占比箱线/热力图、带宽时序、总体TTFT CDF、分类CDF和客户端到达迟到CDF。
缺少某类数据时，校验记录和报告注明原因。输出包含 `report.md`、`figure/`、`report_data/`、`reproduce/`和同名ZIP。

首次生成状态为 `data_validated_visual_pending`。打开全部本次PNG检查后执行：

```bash
python skills/report-organizer/scripts/generate_report.py \
  --finalize temp/report-skill-example --visual-reviewed
```

完成步骤复核全部产物哈希、Markdown链接及ZIP内容。不能用该参数代替实际看图。
分享包包含足够重绘的标准数据、脚本及相对路径；原始日志仅记录来源和哈希，不自动打包。

## 原生日志适配边界

TTFT按网关到达到首token，SLO逐请求匹配deadline，分位数为nearest-rank。
时间线在线分类与输入cohort分开。红色在途IO不是纯依赖等待，灰色不是没有请求。
带宽重建只用于DV单盘、零启动延迟、grouped max-min共享池，并校验实际速率积分与读量。
该模型会继续分配富余带宽，所以申请率不是实际速率的硬上限。其他拓扑导出真实区间，不假设一直满带宽。
