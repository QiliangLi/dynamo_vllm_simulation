# AI接手本实验分支

默认用中文。使用短句，区分实现事实、性能观察和待验证假设。

## 先读与入口

1. 阅读[实验分支使用说明](docs/实验分支使用说明.md)。它定义本分支的可用入口与边界。
2. 阅读[benchmark说明](benchmark/README.md)、[AI用法](benchmark/agent.md)和[Benchmark目录](benchmark/benchmark目录.md)，从目录选择负载。
3. 在`dynamo_vllm_cpu_sim/`内运行`scripts/run_benchmark.py`。先`--smoke`，再正式回放。
4. 修改模型、准入或反馈前，读取主说明中的对应设计文档。
5. 查看既有报告先读 report/README.md、report/agent.md 和 report/报告目录.md。只整理报告不自动重跑实验；归档验证与原运行验收分开。

6. 需要使用共享技能时，先读[skills的AI说明](skills/agent.md)与[技能目录](skills/技能目录.md)，再读对应SKILL.md。报告整理不隐含重新运行或发布实验。

## 必须保持的实验合同

- 可运行Mooncake360 benchmark固定K=1、4秒到达和12个逻辑实例。单共享池带宽读取所选benchmark的config.json和manifest.json，并与名称核对，不写死为90 GB/s。
- AgentX360固定输入另用12秒、180GB/s、12实例及direct_sum/关闭执行反馈；统一入口支持显式长上下文外推和专用启动/验收，读取run_contract.json，不套用Mooncake默认条件。详见benchmark/agentx_random_360_12s_12_180GBps/README.md。
- 人工ABC六秒负载读取synthetic_abc360_v1合同：6秒、90GB/s、固定h/u、direct_sum；首请求前约5.03ms间隔必须保留，不注入AgentX外推参数。
- 冒烟使用2个实例；Mooncake取各类最短/最长，AgentX另取中位共6条且含最长上下文；不能把冒烟当作正式性能数据。
- 正式每组72预热排空后360请求，排除预热分类历史，重新开始正式GC窗口。
- 三种策略共用EDF、模型、存储和反馈模式；α只改变等待重读评分系数。
- 保留原生Dynamo picker、完整资源预留和vLLM token预算/slot分配。
- 输入JSONL的h/u、token值、顺序、到达时刻不能为获得预期结果而修改。
- 人工A/B/C/D标签不等于TFS运行期P75分类。首token释放不是实际IO结束释放。
- GLM5解析计时及存储速率属于仿真；不得表述为Ascend910C实测。

## 修改与验证

- 更新共享核心后，运行`scripts/check_runtime_sync.py`并核对RUNTIME_SYNC.json；共享文件变化须与研究版同步并刷新清单。不要据此把交付专用报告或运行入口覆盖到研究版。

- 修改`sim/`、`scripts/`、`configs/`时同步包内README。行为边界变化时同步两份DESIGN。
- 新结果写入独立输出目录，保留失败记录；不覆盖旧结果，不只保留有利轮次。
- 按改动运行相关测试。发布前运行完整单元测试和主说明中的冒烟/正式验收。
- 验收至少覆盖请求数、h/u/token/字节、模型公式、层屏障、EDF、配额归零及进程退出。
- 核对JSONL及源码SHA；不能用旧配置的T0/期限判断新配置SLO。
- 不提交`.venv/`、`upstream/`、原生构建、大型原始事件或私人工作区历史。
- 仓库中较早的实验脚本和README章节是历史材料。不要绕过新入口去猜测命令。

## Git

只在用户指定的实验分支提交。推送需要用户授权和目标仓库写权限。
不自动向原仓库main推送，不强推，不自动合并其他分支。
