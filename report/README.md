# Report

本目录按 benchmark 归档已完成实验的报告。具体报告、方案、日期和版本关系统一见 [报告目录.md](报告目录.md)。AI 使用约定见 [agent.md](agent.md)。

## 目录与命名

每个 benchmark 使用与 benchmark/ 相同的目录名。每份报告单独放入“YYYYMMDD-HHMMSS_实际方案列表”目录，日期时间采用 Asia/Shanghai，表示报告创建时间。正文另列实际实验日期和可获取的起止时刻，不用文件时间推断实验时间。

方案名写清直接提交的 FCFS/EDF、TFS 的排序与 alpha、强制准入等关键差异。同组方案可用 + 分隔；不要使用 Windows 不允许的 /。seed、重复、完整参数和代码版本放入正文及manifest。

|文件|作用|
|---|---|
|report.md|完整中文报告，先结论后证据，保留0—10节及本次边界|
|figure/|正文使用的原图；按相对路径引用|
|report_data/|汇总/逐请求小表、有效配置、来源记录和可用的标准重绘数据|
|manifest.json|报告ID、study_id、benchmark与输入SHA、创建时间、实验日期、实际方案、版本关系、文件SHA及复现范围|
|archive_validation.json|本次归档校验；与源报告原有验证记录分开|
|reproduce/README.md|说明能够复核/重绘什么、完整重跑缺少什么|

一份报告可以包含多个方案和重复。每轮运行保留标识及失败记录，不将复用结果当作独立复跑。联合研究可以有多个benchmark入口，但study_id保持相同。

## 查阅与校验

从仓库根目录运行；目录和资产校验仅需Python标准库，不启动仿真：

```bash
python report/manage.py list
python report/manage.py verify
python report/manage.py catalog --check
```

source_validation.json保存原报告验证，archive_validation.json验证迁移后的文件、来源表格、图像身份和相对链接。图表数据一致不证明性能排名稳定；模拟计算占比不是硬件利用率。

## 重绘与重跑

manifest声明standard_dataset时，可以使用随仓库提供的固定报告工具重绘到新目录：

```bash
python report/manage.py redraw <benchmark/报告目录> --output <新的输出目录>
```

工具不改变实验，依赖Matplotlib、NumPy、Pillow与中文字体；Windows使用现有WSL环境，Linux直接运行。不自动安装或升级环境。生成的新图需要重新人工检查，不能沿用旧报告的视觉验收。

历史归档可能只有原图和小表，没有当前标准dataset。其旧source_scripts仅作来源资料，不承诺直接运行。完整仿真重跑仍需读取benchmark配置、原运行版本与实验分支使用说明；缺少的原始事件和运行环境必须明确记录。

## 新增报告

增加独立报告目录与manifest，保留来源及失败证据。运行verify --write，再运行catalog和catalog --check。普通新增只更新报告目录，不改本README或agent.md。正文、数据或图发生变化时更新文件SHA并重新验证；不静默覆盖已交付结论。
